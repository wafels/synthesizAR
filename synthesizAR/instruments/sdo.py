"""
Class for the SDO/AIA instrument. Holds information about the cadence and
spatial and spectroscopic resolution.
"""

import os
import logging
import json
import pkg_resources
import warnings

import numpy as np
from scipy.interpolate import splrep, splev, interp1d
from scipy.ndimage import map_coordinates
from scipy.ndimage.filters import gaussian_filter
import astropy.units as u
from sunpy.map import Map
from sunpy.util.metadata import MetaDict
import h5py
try:
    import dask
except ImportError:
    warnings.warn('Dask library not found. You will not be able to use the parallel option.')

import synthesizAR
from synthesizAR.instruments import InstrumentBase, Pair


class InstrumentSDOAIA(InstrumentBase):
    """
    Atmospheric Imaging Assembly object for observing synthesized active
    region emission.

    Parameters
    ----------
    observing_time : `tuple`
        start and end of observing time
    observing_area : `tuple`
        x and y range of observation
    use_temperature_response_functions : `bool`
        if True, do simple counts calculation
    response_function_file : `str`
        filename containing AIA response functions

    Examples
    --------
    Notes
    -----
    """

    fits_template = MetaDict()
    fits_template['telescop'] = 'SDO/AIA'
    fits_template['detector'] = 'AIA'
    fits_template['waveunit'] = 'angstrom'

    name = 'SDO_AIA'
    channels = [
        {'wavelength': 94*u.angstrom, 'telescope_number': 4,
         'gaussian_width': {'x': 0.951*u.pixel, 'y': 0.951*u.pixel}},
        {'wavelength': 131*u.angstrom, 'telescope_number': 1,
         'gaussian_width': {'x': 1.033*u.pixel, 'y': 1.033*u.pixel}},
        {'wavelength': 171*u.angstrom, 'telescope_number': 3,
         'gaussian_width': {'x': 0.962*u.pixel, 'y': 0.962*u.pixel}},
        {'wavelength': 193*u.angstrom, 'telescope_number': 2,
         'gaussian_width': {'x': 1.512*u.pixel, 'y': 1.512*u.pixel}},
        {'wavelength': 211*u.angstrom, 'telescope_number': 2,
         'gaussian_width': {'x': 1.199*u.pixel, 'y': 1.199*u.pixel}},
        {'wavelength': 335*u.angstrom, 'telescope_number': 1,
         'gaussian_width': {'x': 0.962*u.pixel, 'y': 0.962*u.pixel}}]

    cadence = 10.0*u.s
    resolution = Pair(0.600698*u.arcsec/u.pixel, 0.600698*u.arcsec/u.pixel, None)

    def __init__(self, observing_time, observing_area=None,
                 use_temperature_response_functions=True, apply_psf=True):
        super().__init__(observing_time, observing_area)
        self.apply_psf = apply_psf
        self.use_temperature_response_functions = use_temperature_response_functions
        self._setup_channels()

    def _setup_channels(self):
        """
        Setup channel, specifically the wavelength or temperature response functions.

        Notes
        -----
        This should be replaced once the response functions are available in SunPy. Probably should
        configure wavelength response function interpolators also.
        """
        aia_fn = pkg_resources.resource_filename('synthesizAR', 'instruments/data/sdo_aia.json')
        with open(aia_fn, 'r') as f:
            aia_info = json.load(f)

        for channel in self.channels:
            channel['name'] = str(channel['wavelength'].value).strip('.0')
            channel['instrument_label'] = '{}_{}'.format(self.fits_template['detector'],
                                                         channel['telescope_number'])
            channel['wavelength_range'] = None
            x = aia_info[channel['name']]['temperature_response_x']
            y = aia_info[channel['name']]['temperature_response_y']
            channel['temperature_response_spline'] = splrep(x, y)
            x = aia_info[channel['name']]['response_x']
            y = aia_info[channel['name']]['response_y']
            channel['wavelength_response_spline'] = splrep(x, y)

    def build_detector_file(self, file_template, dset_shape, chunks, *args, parallel=False):
        """
        Allocate space for counts data.
        """
        additional_fields = ['{}'.format(channel['name']) for channel in self.channels]
        super().build_detector_file(file_template, dset_shape, chunks, *args, additional_fields=additional_fields, 
                                    parallel=parallel)
        
    @staticmethod
    def calculate_counts_simple(channel, loop, *args):
        """
        Calculate the AIA intensity using only the temperature response functions.
        """
        response_function = (splev(np.ravel(loop.electron_temperature), channel['temperature_response_spline'])
                             * u.count*u.cm**5/u.s/u.pixel)
        counts = np.reshape(np.ravel(loop.density**2)*response_function, loop.density.shape)
        return counts

    @staticmethod
    def flatten_emissivities(channel, emission_model):
        """
        Compute product between wavelength response and emissivity for all ions
        """
        flattened_emissivities = []
        for ion in emission_model:
            wavelength, emissivity = emission_model.get_emissivity(ion)
            if wavelength is None or emissivity is None:
                flattened_emissivities.append(None)
                continue
            interpolated_response = splev(wavelength.value, channel['wavelength_response_spline'], ext=1)
            em_summed = np.dot(emissivity.value, interpolated_response)
            unit = emissivity.unit*u.count/u.photon*u.steradian/u.pixel*u.cm**2
            flattened_emissivities.append(u.Quantity(em_summed, unit))

        return flattened_emissivities

    @staticmethod
    def calculate_counts_full(channel, loop, emission_model, flattened_emissivities):
        """
        Calculate the AIA intensity using the wavelength response functions and a 
        full emission model.
        """
        density = loop.density
        electron_temperature = loop.electron_temperature
        counts = np.zeros(electron_temperature.shape)
        itemperature, idensity = emission_model.interpolate_to_mesh_indices(loop)
        for ion, flat_emiss in zip(emission_model, flattened_emissivities):
            if flat_emiss is None:
                continue
            ionization_fraction = emission_model.get_ionization_fraction(loop, ion)
            tmp = np.reshape(map_coordinates(flat_emiss.value, np.vstack([itemperature, idensity])),
                             electron_temperature.shape)
            tmp = u.Quantity(np.where(tmp < 0., 0., tmp), flat_emiss.unit)
            counts_tmp = ion.abundance*0.83/(4*np.pi*u.steradian)*ionization_fraction*density*tmp
            if not hasattr(counts, 'unit'):
                counts = counts*counts_tmp.unit
            counts += counts_tmp

        return counts
    
    def flatten_serial(self, loops, interpolated_loop_coordinates, hf, emission_model=None):
        """
        Interpolate intensity in each channel to temporal resolution of the instrument
        and appropriate spatial scale.
        """
        simple = self.use_temperature_response_functions or emission_model is None 
        if simple:
            calculate_counts = self.calculate_counts_simple
        else:
            calculate_counts = self.calculate_counts_full
        
        for channel in self.channels:
            start_index = 0
            dset = hf[channel['name']]
            flattened_emissivities = [] if simple else self.flatten_emissivities(channel, emission_model)
            for loop, interp_s in zip(loops, interpolated_loop_coordinates):
                y = self.interpolate_and_store(calculate_counts(channel, loop, emission_model, flattened_emissivities),
                                               loop, self.observing_time, interp_s)
                synthesizAR.Observer.commit(y, dset, start_index)
                start_index += interp_s.shape[0]

    def flatten_parallel(self, loops, interpolated_loop_coordinates, save_path, emission_model=None):
        """
        Interpolate intensity in each channel to temporal resolution of the instrument
        and appropriate spatial scale. Returns a dask task.
        """
        simple = self.use_temperature_response_functions or emission_model is None
        calculate_counts = self.calculate_counts_simple if simple else self.calculate_counts_full

        tasks = {}
        for channel in self.channels:
            tasks[channel['name']] = []
            flattened_emissivities = [] if simple else dask.delayed(self.flatten_emissivities)(channel, emission_model)
            for loop, interp_s in zip(loops, interpolated_loop_coordinates):
                y = dask.delayed(calculate_counts)(channel, loop, emission_model, flattened_emissivities)
                tmp_path = save_path.format(channel['name'], loop.name)
                task = dask.delayed(self.interpolate_and_store)(y, loop, self.observing_time, interp_s, tmp_path)
                tasks[channel['name']].append(task)

        return tasks

    @staticmethod
    def _detect(counts_filename, channel, i_time, header, bins, bin_range, apply_psf):
        """
        For a given channel and timestep, map the intensity along the loop to the 3D field and
        return the AIA data product.

        Parameters
        ----------
        counts_filename : `str`
        channel : `dict`
        i_time : `int`
        header : `~sunpy.util.metadata.MetaDict`
        bins : `Pair`
        bin_range : `Pair`
        apply_psf : `bool`

        Returns
        -------
        AIA data product : `~sunpy.Map`
        """
        with h5py.File(counts_filename, 'r') as hf:
            weights = np.array(hf[channel['name']][i_time, :])
            units = u.Unit(hf[channel['name']].attrs['units'])
            coordinates = u.Quantity(hf['coordinates'], hf['coordinates'].attrs['units'])

        hist, edges = np.histogramdd(coordinates.value, bins=bins, range=bin_range, weights=weights)
        header['bunit'] = (units*coordinates.unit).to_string()
        counts = np.dot(hist, np.diff(edges[2])).T

        if apply_psf:
            counts = gaussian_filter(counts, (channel['gaussian_width']['y'].value,
                                              channel['gaussian_width']['x'].value))
        return Map(counts, header)

    def detect(self, channel, i_time, field, parallel=False):
        header = self.make_fits_header(field, channel)
        parameters = (self.counts_file, channel, i_time, header, self.bins, self.bin_range, self.apply_psf)
        if parallel:
            return dask.delayed(self._detect)(*parameters)
        else:
            return self._detect(*parameters)

