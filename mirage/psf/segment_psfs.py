#! /usr/bin/env python

"""This module contains code to make, locate, and parse the appropriate segment
PSF library files to use for a given simulation. These files are made using
the WebbPSF psf_grid() method, and are turned into photutils GriddedPSFModel
objects using webbpsf.utils.to_griddedpsfmodel.

Author
------

    - Lauren Chambers

Use
---

    This module can be imported and called as such:
    ::
        from mirage.psf.segment_psfs import get_gridded_segment_psf_library_list
        lib = get_gridded_segment_psf_library_list(instrument, detector, filter,
                out_dir, pupilname="CLEAR")
"""
import os
import time

from astropy.io import fits
import numpy as np
import pysiaf
import webbpsf
from webbpsf.gridded_library import CreatePSFLibrary
from webbpsf.utils import to_griddedpsfmodel

import multiprocessing
import functools

from mirage.psf.psf_selection import get_library_file


def _generate_psfs_for_one_segment(nrc_inst, ote, segment_tilts, out_dir, boresight, lib, detectors, filters, fov_pixels, nlambda, overwrite, i):
    """
    Helper function for parallelized segment PSF calculations

	For use with multiprocessing.Pool, the iterable argument must be in the last position

	See doc string of generate_segment_psfs for input parameter definitions.

	"""
    i_segment = i + 1

    segname = webbpsf.webbpsf_core.segname(i_segment)
    print('GENERATING SEGMENT {} DATA'.format(segname))

    det_filt_match = False
    for det in sorted(detectors):
        for filt in list(filters):
            # Make sure the detectors and filters match
            if (det in lib.nrca_short_detectors and filt not in lib.nrca_short_filters) \
                    or (det in lib.nrca_long_detectors and filt not in lib.nrca_long_filters):
                continue

            det_filt_match = True

            # Define the filter and detector
            nrc_inst.filter = filt
            nrc_inst.detector = det

            # Restrict the pupil to the current segment
            pupil = webbpsf.webbpsf_core.one_segment_pupil(i_segment)
            ote.amplitude = pupil[0].data
            nrc_inst.pupil = ote

            # Generate the PSF grid
            # NOTE: we are choosing a polychromatic simulation here to better represent the
            # complexity of simulating unstacked PSFs. See the WebbPSF website for more details.
            grid = nrc_inst.psf_grid(num_psfs=1, save=False, all_detectors=False,
                               use_detsampled_psf=True, fov_pixels=fov_pixels,
                               oversample=1, overwrite=overwrite, add_distortion=False,
                               nlambda=nlambda, verbose=False)

            # Remove and add header keywords about segment
            del grid.meta["grid_xypos"]
            del grid.meta["oversampling"]
            grid.meta['SEGID'] = (i_segment, 'ID of the mirror segment')
            grid.meta['SEGNAME'] = (segname, 'Name of the mirror segment')
            grid.meta['XTILT'] = (round(segment_tilts[i, 0], 2), 'X tilt of the segment in micro radians')
            grid.meta['YTILT'] = (round(segment_tilts[i, 1], 2), 'Y tilt of the segment in micro radians')
            grid.meta['SMPISTON'] = (ote.segment_state[18][4], 'Secondary mirror piston (defocus) in microns')

            if boresight is not None:
                grid.meta['BSOFF_V2'] = (boresight[0], 'Telescope boresight offset in V2 in arcminutes')
                grid.meta['BSOFF_V3'] = (boresight[1], 'Telescope boresight offset in V3 in arcminutes')

            # Write out file
            filename = 'nircam_{}_{}_fovp{}_samp1_npsf1_seg{:02d}.fits'.format(det.lower(), filt.lower(),
                                                                               fov_pixels, i_segment)
            filepath = os.path.join(out_dir, filename)
            primaryhdu = fits.PrimaryHDU(grid.data)
            tuples = [(a, b, c) for (a, (b, c)) in sorted(grid.meta.items())]
            primaryhdu.header.extend(tuples)
            hdu = fits.HDUList(primaryhdu)
            hdu.writeto(filepath, overwrite=overwrite)
            print('Saved gridded library file to {}'.format(filepath))

    if det_filt_match == False:
        raise ValueError('No matching filters and detectors given - all '
                         'filters are longwave but detectors are shortwave, '
                         'or vice versa.')


def generate_segment_psfs(ote, segment_tilts, out_dir, filters=['F212N', 'F480M'],
                          detectors='all', fov_pixels=1024, boresight=None, overwrite=False,
                          segment=None, jitter=None, nlambda=10, nrc_options=None):
    """Generate NIRCam PSF libraries for all 18 mirror segments given a perturbed OTE
    mirror state. Saves each PSF library as a FITS file named in the following format:
        nircam_{filter}_fovp{fov size}_samp1_npsf1_seg{segment number}.fits

    Parameters
    ----------
    ote : webbpsf.opds.OTE_Linear_Model_WSS object
        WebbPSF OTE object describing perturbed OTE state with tip and tilt removed

    segment_tilts : numpy.ndarray
        List of X and Y tilts for each mirror segment, in microradians

    out_dir : str
        Directory in which to save FITS files

    filters : str or list, optional
        Which filters to generate PSF libraries for. Default is ['F212N', 'F480M']
        (the two filters used for most commissioning activities).

    detectors : str or list, optional
        Which detectors to generate PSF libraries for. Default is 'all'.

    fov_pixels : int, optional
        Size of the PSF to generate, in pixels. Default is 1024.

    boresight: list, optional
        Telescope boresight offset in V2/V3 in arcminutes. This offset is added on top of the individual
        segment tip/tilt values.

    overwrite : bool, optional
        True/False boolean to overwrite the output file if it already
        exists. Default is True.

    segment : int or list
        The mirror segment number or list of numbers for which to generate PSF libraries

    jitter : float
        Jitter value to use in the call to webbpsf when generating PSF library. If None
        (default) the nominal jitter (7mas radial) is used.

    nlambda : int
        Number of wavelengths to use for polychromatic PSF calculations.

    nrc_options : dict
        Optional; additional options to set on the NIRCam class instance used in this function.
        Any items in this dict will be added into the .options dict prior to the PSF calculations.
    """
    # Create webbpsf NIRCam instance
    nc = webbpsf.NIRCam()

    # Create dummy CreatePSFLibrary instance to get lists of filter and detectors
    lib = CreatePSFLibrary

    # Define the filter list to loop through
    if isinstance(filters, str):
        filters = [filters]
    elif not isinstance(filters, list):
        raise TypeError('Please define filters as a string or list, not {}'.format(type(filters)))

    # Define the detector list to loop through
    if detectors == 'all':
        detectors = ['NRCA1', 'NRCA2', 'NRCA3', 'NRCA4', 'NRCA5',
                     'NRCB1', 'NRCB2', 'NRCB3', 'NRCB4', 'NRCB5',]
    elif isinstance(detectors, str):
        detectors = [detectors]
    elif not isinstance(detectors, list):
        raise TypeError('Please define detectors as a string or list, not {}'.format(type(detectors)))

    # Make sure segment is a list
    segments = list(range(18))
    if segment is not None:
        if isinstance(segment, int):
            segments = [segment]
        elif isinstance(segment, list):
            segments = segment
        else:
            raise ValueError("segment keyword must be either an integer or list of integers.")

    # Allow for non-nominal jitter values
    if jitter is not None:
        if isinstance(jitter, float):
            nc.options['jitter'] = 'gaussian'
            nc.options['jitter_sigma'] = jitter
            print('Adding jitter', jitter)
        elif isinstance(jitter, str):
            allowed_strings = ['PCS=Coarse_Like_ITM', 'PCS=Coarse']
            if jitter in allowed_strings:
                nc.options['jitter'] = jitter
                print('Adding {} jitter'.format(jitter))
            else:
                print("Invalid jitter string. Must be one of: {}. Ignoring and using defaults.".format(allowed_strings))
        else:
            print("Wrong input to jitter, assuming defaults")
    if nrc_options is not None:
        nc.options.update(nrc_options)

	# Set up multiprocessing pool
    nproc = min(multiprocessing.cpu_count() // 2,18)      # number of procs could be optimized further here. TBD.
                                                  # some parts of PSF calc are themselves parallelized so using
                                                  # fewer processes than number of cores is likely reasonable.
    pool = multiprocessing.Pool(processes=nproc)
    print(f"Will perform parallelized calculation using {nproc} processes")

    # Set up a function instance with most arguments fixed
    calc_psfs_for_one_segment = functools.partial(_generate_psfs_for_one_segment, nc, ote, segment_tilts, out_dir, boresight, lib, detectors,
                                              filters, fov_pixels, nlambda, overwrite)

    # Create PSF grids for all requested segments, detectors, and filters
    pool_start_time = time.time()
    results = pool.map(calc_psfs_for_one_segment, segments)
    pool_stop_time = time.time()
    print('\n=========== Elapsed time (all segments):', pool_stop_time - pool_start_time, '============\n')
    pool.close()


def get_gridded_segment_psf_library_list(instrument, detector, filtername,
                                         library_path, pupilname="CLEAR"):
    """Find the filenames for the appropriate gridded segment PSF libraries and
    read them into griddedPSFModel objects

    Parameters
    ----------
    instrument : str
        Name of instrument the PSFs are from

    detector : str
        Name of the detector within ```instrument```

    filtername : str
        Name of filter used for PSF library creation

    library_path : str
        Path pointing to the location of the PSF library

    pupilname : str, optional
        Name of pupil wheel element used for PSF library creation. Default is "CLEAR".

    Returns:
    --------
    libraries : list of photutils.griddedPSFModel
        List of object containing segment PSF libraries

    """
    library_list = get_segment_library_list(instrument, detector, filtername, library_path, pupil=pupilname)

    print("Segment PSFs will be generated using:")
    for filename in library_list:
        print(os.path.basename(filename))

    libraries = []
    for filename in library_list:
        with fits.open(filename) as hdulist:
        #     hdr = hdulist[0].header
        #     d = hdulist[0].data
        #
        # data = d[0][0]
        # phdu = fits.PrimaryHDU(data, header=hdr)
        # hdulist = fits.HDUList(phdu)

            lib_model = to_griddedpsfmodel(hdulist)
            libraries.append(lib_model)

    return libraries


def get_segment_library_list(instrument, detector, filt,
                             library_path, pupil='CLEAR'):
    """Given an instrument and filter name along with the path of
    the PSF library, find the appropriate 18 segment PSF library files.

    Parameters
    -----------
    instrument : str
        Name of instrument the PSFs are from

    detector : str
        Name of the detector within ```instrument```

    filt : str
        Name of filter used for PSF library creation

    library_path : str
        Path pointing to the location of the PSF library

    pupil : str, optional
        Name of pupil wheel element used for PSF library creation. Default is
        'CLEAR'.

    segment_id : int or None, optional
        If specified, returns a segment PSF library file and denotes the ID
        of the mirror segment

    Returns
    --------
    library_list : list
        List of the names of the segment PSF library files for the instrument
        and filter name
    """
    library_list = []
    for seg_id in np.arange(1, 19):
         segment_file = get_library_file(
             instrument, detector, filt, pupil, '', 0, library_path,
             segment_id=seg_id
         )
         library_list.append(segment_file)

    return library_list


def get_segment_offset(segment_number, detector, library_list):
    """Convert vectors coordinates in the local segment control
    coordinate system to NIRCam detector X and Y coordinates,
    at least proportionally, in order to calculate the location
    of the segment PSFs on the given detector.

    Parameters
    ----------
    segment : int
        Segment ID, i.e 3
    detector : str
        Name of NIRCam detector
    library_list : list
        List of the names of the segment PSF library files

    Returns
    -------
    x_arcsec
        The x offset of the segment PSF in arcsec
    y_arcsec
        The y offset of the segment PSF in arcsec
    """

    # Verify that the segment number in the header matches the index
    seg_index = int(segment_number) - 1
    header = fits.getheader(library_list[seg_index])

    assert int(header['SEGID']) == int(segment_number), \
        "Uh-oh. The segment ID of the library does not match the requested " \
        "segment. The library_list was not assembled correctly."
    xtilt = header['XTILT']
    ytilt = header['YTILT']
    segment = header['SEGNAME'][:2]
    sm_piston = header.get('SMPISTON',0)

    # SM piston has, as one of its effects, adding tilt onto each segment,
    # along with higher order WFE such as defocus. We model here the effect
    # of SM piston onto the x and y offsets.
    # Coefficients determined based on WAS influence function matrix, as
    # derived from segment control geometries.
    if segment.startswith('A'):
        xtilt += sm_piston * 0.010502
    elif segment.startswith('B'):
        xtilt += sm_piston * -0.020093
    elif segment.startswith('C'):
        ytilt += sm_piston * 0.017761

    control_xaxis_rotations = {
        'A1': 180, 'A2': 120, 'A3': 60, 'A4': 0, 'A5': -60,
        'A6': -120, 'B1': 0, 'C1': 60, 'B2': -60, 'C2': 0,
        'B3': -120, 'C3': -60, 'B4': -180, 'C4': -120,
        'B5': -240, 'C5': -180, 'B6': -300, 'C6': -240
    }

    x_rot = control_xaxis_rotations[segment]  # degrees
    x_rot_rad = x_rot * np.pi / 180  # radians

    # Note that y is defined as the x component and x is defined as the y component.
    # This is because "xtilt" moves the PSF in the y direction, and vice versa.
    tilt_onto_y = (xtilt * np.cos(x_rot_rad)) - (ytilt * np.sin(x_rot_rad))
    tilt_onto_x = (xtilt * np.sin(x_rot_rad)) + (ytilt * np.cos(x_rot_rad))

    umrad_to_arcsec = 1e-6 * (180./np.pi) * 3600
    x_arcsec = 2 * umrad_to_arcsec * tilt_onto_x
    y_arcsec = 2 * umrad_to_arcsec * tilt_onto_y

    try:
        x_arcsec -= header['BSOFF_V2']*60 # BS offset values in header are in arcminutes
        y_arcsec += header['BSOFF_V3']*60 #
        print("Added a telescope boresight offset based on header information")
    except:
        pass

    return x_arcsec, y_arcsec
