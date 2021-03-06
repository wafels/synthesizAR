"""
Create data products from loop simulations
"""
import os
import warnings
import logging
import itertools
import toolz

import numpy as np
from scipy.interpolate import splev, splprep, interp1d
import scipy.ndimage
import astropy.units as u
from sunpy.sun import constants
from sunpy.coordinates.frames import HeliographicStonyhurst
import h5py
try:
    import distributed
except ImportError:
    warnings.warn('Dask distributed scheduler required for parallel execution')


class Observer(object):
    """
    Class for assembling AR from loops and creating data products from 2D projections.

    Parameters
    ----------
    field : `~synthesizAR.Field`
    instruments : `list`
    parallel : `bool`

    Examples
    --------
    """

    def __init__(self, field, instruments, parallel=False):
        self.parallel = parallel
        self.field = field
        self.instruments = instruments
        self._channels_setup()
        
    def _channels_setup(self):
        """
        Tell each channel of each detector which wavelengths fall in it.
        """
        for instr in self.instruments:
            for channel in instr.channels:
                if channel['wavelength_range'] is not None:
                    channel['model_wavelengths'] = []
                    for wvl in self.field.loops[0].resolved_wavelengths:
                        if channel['wavelength_range'][0] <= wvl <= channel['wavelength_range'][-1]:
                            channel['model_wavelengths'].append(wvl)
                    if channel['model_wavelengths']:
                        channel['model_wavelengths'] = u.Quantity(channel['model_wavelengths'])

    @u.quantity_input
    def _interpolate_loops(self, ds: u.cm):
        """
        Interpolate all loops to a resolution (`ds`) below the minimum bin width
        of all of the instruments. This ensures that the image isn't 'patchy'
        when it is binned.
        """
        # Interpolate all loops in HEEQ coordinates
        total_coordinates = []
        interpolated_loop_coordinates = []
        for loop in self.field.loops:
            n_interp = int(np.ceil((loop.full_length/ds).decompose()))
            interpolated_s = np.linspace(loop.field_aligned_coordinate.value[0],
                                         loop.field_aligned_coordinate.value[-1], n_interp)
            interpolated_loop_coordinates.append(interpolated_s)
            nots, _ = splprep(loop.coordinates.cartesian.xyz.value)
            total_coordinates.append(np.array(splev(np.linspace(0, 1, n_interp), nots)).T)

        total_coordinates = np.vstack(total_coordinates) * loop.coordinates.cartesian.xyz.unit

        return total_coordinates, interpolated_loop_coordinates

    def build_detector_files(self, savedir, ds, **kwargs):
        """
        Create files to store interpolated counts before binning.

        .. note:: After creating the instrument objects and passing them to the observer,
                  it is always necessary to call this method.
        """
        file_template = os.path.join(savedir, '{}_counts.h5')
        total_coordinates, self._interpolated_loop_coordinates = self._interpolate_loops(ds)
        interp_s_shape = (int(np.median([s.shape for s in self._interpolated_loop_coordinates])),)
        for instr in self.instruments:
            chunks = kwargs.get('chunks', instr.observing_time.shape + interp_s_shape)
            dset_shape = instr.observing_time.shape + (len(total_coordinates),)
            instr.build_detector_file(file_template, dset_shape, chunks, self.field,
                                      parallel=self.parallel, **kwargs)
            with h5py.File(instr.counts_file, 'a') as hf:
                if 'coordinates' not in hf:
                    dset = hf.create_dataset('coordinates', data=total_coordinates.value)
                    dset.attrs['units'] = total_coordinates.unit.to_string()

    def flatten_detector_counts(self, **kwargs):
        """
        Calculate intensity for each loop, interpolate it to the appropriate spatial and temporal
        resolution, and store it. This is done either in serial or parallel.
        """
        if self.parallel:
            return self._flatten_detector_counts_parallel(**kwargs)
        else:
            self._flatten_detector_counts_serial(**kwargs)

    def _flatten_detector_counts_serial(self, **kwargs):
        emission_model = kwargs.get('emission_model', None)
        interpolate_hydro_quantities = kwargs.get('interpolate_hydro_quantities', True)
        for instr in self.instruments:
            with h5py.File(instr.counts_file, 'a', driver=kwargs.get('hdf5_driver', None)) as hf:
                start_index = 0
                if interpolate_hydro_quantities:
                    for interp_s, loop in zip(self._interpolated_loop_coordinates, self.field.loops):
                        for q in ['velocity_x', 'velocity_y', 'velocity_z', 'electron_temperature',
                                  'ion_temperature', 'density']:
                            val = instr.interpolate_and_store(q, loop, interp_s)
                            instr.commit(val, hf[q], start_index)
                        start_index += interp_s.shape[0]
                instr.flatten_serial(self.field.loops, self._interpolated_loop_coordinates, hf,
                                     emission_model=emission_model)

    def _flatten_detector_counts_parallel(self, **kwargs):
        """
        Build custom Dask graph interpolating quantities for each in loop in time and space.
        """
        client = distributed.get_client()
        emission_model = kwargs.get('emission_model', None)
        interpolate_hydro_quantities = kwargs.get('interpolate_hydro_quantities', True)
        futures = {}
        start_indices = np.insert(np.array(
            [s.shape[0] for s in self._interpolated_loop_coordinates]).cumsum()[:-1], 0, 0)
        for instr in self.instruments:
            # Create temporary files where interpolated results will be written
            tmp_dir = os.path.join(os.path.dirname(instr.counts_file), 'tmp_parallel_files')
            if not os.path.exists(tmp_dir):
                os.makedirs(tmp_dir)
            interp_futures = []
            if interpolate_hydro_quantities:
                for q in ['velocity_x', 'velocity_y', 'velocity_z', 'electron_temperature',
                          'ion_temperature', 'density']:
                    partial_interp = toolz.curry(instr.interpolate_and_store)(
                        q, save_dir=tmp_dir, dset_name=q)
                    loop_futures = client.map(partial_interp, self.field.loops,
                                              self._interpolated_loop_coordinates, start_indices)
                    # Block until complete
                    distributed.client.wait([loop_futures])
                    interp_futures += loop_futures

            # Calculate and interpolate channel counts for instrument
            counts_futures = instr.flatten_parallel(self.field.loops,
                                                    self._interpolated_loop_coordinates,
                                                    tmp_dir, emission_model=emission_model)
            # Assemble into file and clean up
            assemble_future = client.submit(instr.assemble_arrays, interp_futures+counts_futures,
                                            instr.counts_file)
            futures[f'{instr.name}'] = client.submit(self._cleanup, assemble_future)

        return futures

    @staticmethod
    def _cleanup(filenames):
        for f in filenames:
            os.remove(f)
        os.rmdir(os.path.dirname(f))

    @staticmethod
    def assemble_map(observed_map, filename, time):
        observed_map.meta['tunit'] = time.unit.to_string()
        observed_map.meta['t_obs'] = time.value
        observed_map.save(filename)

    def bin_detector_counts(self, savedir, **kwargs):
        """
        Assemble pipelines for building maps at each timestep.

        Build pipeline for computing final synthesized data products. This can be done
        either in serial or parallel.

        Parameters
        ----------
        savedir : `str`
            Top level directory to save data products in
        """
        if self.parallel:
            futures = {instr.name: {} for instr in self.instruments}
            client = distributed.get_client()
        else:
            futures = None
        file_path_template = os.path.join(savedir, '{}', '{}', 'map_t{:06d}.fits')
        for instr in self.instruments:
            bins, bin_range = instr.make_detector_array(self.field)
            with h5py.File(instr.counts_file, 'r') as hf:
                reference_time = u.Quantity(hf['time'], hf['time'].attrs['units'])
            indices_time = [np.where(reference_time == time)[0][0] for time in instr.observing_time]
            for channel in instr.channels:
                header = instr.make_fits_header(self.field, channel)
                file_paths = [file_path_template.format(instr.name, channel['name'], i_time)
                              for i_time in indices_time]
                if not os.path.exists(os.path.dirname(file_paths[0])):
                    os.makedirs(os.path.dirname(file_paths[0]))
                # Parallel
                if self.parallel:
                    partial_detect = toolz.curry(instr.detect)(
                        channel, header=header, bins=bins, bin_range=bin_range)
                    map_futures = client.map(partial_detect, indices_time)
                    futures[instr.name][channel['name']] = client.map(
                        self.assemble_map, map_futures, file_paths, instr.observing_time)
                    distributed.client.wait(futures[instr.name][channel['name']])
                # Serial
                else:
                    for i, i_time in enumerate(indices_time):
                        raw_map = instr.detect(channel, i_time, header, bins, bin_range)
                        self.assemble_map(raw_map, file_paths[i], instr.observing_time[i])

        return futures
