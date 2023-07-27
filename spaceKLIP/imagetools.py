from __future__ import division

import matplotlib
matplotlib.rcParams.update({'font.size': 14})


# =============================================================================
# IMPORTS
# =============================================================================

import os
import pdb
import sys

import astropy.io.fits as pyfits
import matplotlib.pyplot as plt
import numpy as np

import json
import pysiaf
import webbpsf_ext

from copy import deepcopy
from jwst.pipeline import Detector1Pipeline, Image2Pipeline, Coron3Pipeline
from scipy.ndimage import gaussian_filter, median_filter
from scipy.ndimage import shift as spline_shift
from scipy.optimize import leastsq, minimize
from skimage.registration import phase_cross_correlation
from spaceKLIP import utils as ut
from spaceKLIP.psf import JWST_PSF
from spaceKLIP.xara import core
from webbpsf_ext import robust
from webbpsf_ext.coords import dist_image
from webbpsf_ext.webbpsf_ext_core import _transmission_map

import logging
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


# =============================================================================
# MAIN
# =============================================================================

# Load NIRCam true mask centers and filter-dependent shifts from Jarron.
path = 'resources/crpix_jarron.json'
path = os.path.join(os.path.split(os.path.abspath(__file__))[0], path)
file = open(path, 'r')
crpix_jarron = json.load(file)
file.close()
path = 'resources/filter_shifts_jarron.json'
path = os.path.join(os.path.split(os.path.abspath(__file__))[0], path)
file = open(path, 'r')
filter_shifts_jarron = json.load(file)
file.close()

class ImageTools():
    """
    The spaceKLIP image manipulation tools class.
    
    """
    
    def __init__(self,
                 database):
        """
        Initialize the spaceKLIP image manipulation tools class.
        
        Parameters
        ----------
        database : spaceKLIP.Database
            SpaceKLIP database on which the image manipulation steps shall be
            run.
        
        Returns
        -------
        None.
        
        """
        
        # Make an internal alias of the spaceKLIP database class.
        self.database = database
        
        pass
    
    def remove_frames(self,
                      index=[0],
                      types=['SCI', 'SCI_BG', 'REF', 'REF_BG'],
                      subdir='removed'):
        """
        Remove individual frames from the data.
        
        Parameters
        ----------
        index : int or list of int or dict of list of list of int, optional
            Indices (0-indexed) of the frames to be removed. If int, then only
            a single frame will be removed from each observation. If list of
            int, then multiple frames can be removed from each observation. If
            dict of list of list of int, then the dictionary keys must match
            the keys of the observations database, and the number of entries in
            the lists must match the number of observations in the
            corresponding concatenation. Then, a different list of int can be
            used for each individual observation to remove different frames.
            The default is [0].
        types : list of str, optional
            List of data types from which the frames shall be removed. The
            default is ['SCI', 'SCI_BG', 'REF', 'REF_BG'].
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'removed'.
        
        Returns
        -------
        None.
        
        """
        
        # Check input.
        if isinstance(index, int):
            index = [index]
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            nfitsfiles = len(self.database.obs[key])
            for j in range(nfitsfiles):
                
                # Read FITS file and PSF mask.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                nints = self.database.obs[key]['NINTS'][j]
                
                # Skip file types that are not in the list of types.
                if self.database.obs[key]['TYPE'][j] in types:
                    
                    # Remove frames.
                    head, tail = os.path.split(fitsfile)
                    log.info('  --> Frame removal: ' + tail)
                    try:
                        index_temp = index[key][j]
                    except:
                        index_temp = index.copy()
                    log.info('  --> Frame removal: removing frame(s) ' + str(index_temp))
                    data = np.delete(data, index_temp, axis=0)
                    erro = np.delete(erro, index_temp, axis=0)
                    pxdq = np.delete(pxdq, index_temp, axis=0)
                    if imshifts is not None:
                        imshifts = np.delete(imshifts, index_temp, axis=0)
                    if maskoffs is not None:
                        maskoffs = np.delete(maskoffs, index_temp, axis=0)
                    nints = data.shape[0]
                
                # Write FITS file and PSF mask.
                head_pri['NINTS'] = nints
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                self.database.update_obs(key, j, fitsfile, maskfile, nints=nints)
        
        pass
    
    def crop_frames(self,
                    npix=1,
                    types=['SCI', 'SCI_BG', 'REF', 'REF_BG'],
                    subdir='cropped'):
        """
        Crop all frames.
        
        Parameters
        ----------
        npix : int or list of four int, optional
            Number of pixels to be cropped from the frames. If int, the same
            number of pixels will be cropped on each side. If list of four int,
            a different number of pixels can be cropped from the [left, right,
            bottom, top] of the frames. The default is 1.
        types : list of str, optional
            List of data types from which the frames shall be cropped. The
            default is ['SCI', 'SCI_BG', 'REF', 'REF_BG'].
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'cropped'.
        
        Returns
        -------
        None.
        
        """
        
        # Check input.
        if isinstance(npix, int):
            npix = [npix, npix, npix, npix]  # left, right, bottom, top
        if len(npix) != 4:
            raise UserWarning('Parameter npix must either be an int or a list of four int (left, right, bottom, top)')
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            nfitsfiles = len(self.database.obs[key])
            for j in range(nfitsfiles):
                
                # Read FITS file and PSF mask.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                crpix1 = self.database.obs[key]['CRPIX1'][j]
                crpix2 = self.database.obs[key]['CRPIX2'][j]
                
                # Skip file types that are not in the list of types.
                if self.database.obs[key]['TYPE'][j] in types:
                    
                    # Crop frames.
                    head, tail = os.path.split(fitsfile)
                    log.info('  --> Frame cropping: ' + tail)
                    sh = data.shape
                    data = data[:, npix[2]:-npix[3], npix[0]:-npix[1]]
                    erro = erro[:, npix[2]:-npix[3], npix[0]:-npix[1]]
                    pxdq = pxdq[:, npix[2]:-npix[3], npix[0]:-npix[1]]
                    if mask is not None:
                        mask = mask[npix[2]:-npix[3], npix[0]:-npix[1]]
                    crpix1 -= npix[0]
                    crpix2 -= npix[2]
                    log.info('  --> Frame cropping: old shape = ' + str(sh[1:]) + ', new shape = ' + str(data.shape[1:]))
                
                # Write FITS file and PSF mask.
                head_sci['CRPIX1'] = crpix1
                head_sci['CRPIX2'] = crpix2
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                self.database.update_obs(key, j, fitsfile, maskfile, crpix1=crpix1, crpix2=crpix2)
        
        pass
    
    def pad_frames(self,
                   npix=1,
                   cval=np.nan,
                   types=['SCI', 'SCI_BG', 'REF', 'REF_BG'],
                   subdir='padded'):
        """
        Pad all frames.
        
        Parameters
        ----------
        npix : int or list of four int, optional
            Number of pixels to be padded around the frames. If int, the same
            number of pixels will be padded on each side. If list of four int,
            a different number of pixels can be padded on the [left, right,
            bottom, top] of the frames. The default is 1.
        cval : float, optional
            Fill value for the padded pixels. The default is nan.
        types : list of str, optional
            List of data types from which the frames shall be padded. The
            default is ['SCI', 'SCI_BG', 'REF', 'REF_BG'].
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'padded'.
        
        Returns
        -------
        None.
        
        """
        
        # Check input.
        if isinstance(npix, int):
            npix = [npix, npix, npix, npix]  # left, right, bottom, top
        if len(npix) != 4:
            raise UserWarning('Parameter npix must either be an int or a list of four int (left, right, bottom, top)')
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            nfitsfiles = len(self.database.obs[key])
            for j in range(nfitsfiles):
                
                # Read FITS file and PSF mask.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                crpix1 = self.database.obs[key]['CRPIX1'][j]
                crpix2 = self.database.obs[key]['CRPIX2'][j]
                
                # Skip file types that are not in the list of types.
                if self.database.obs[key]['TYPE'][j] in types:
                    
                    # Crop frames.
                    head, tail = os.path.split(fitsfile)
                    log.info('  --> Frame padding: ' + tail)
                    sh = data.shape
                    data = np.pad(data, ((0, 0), (npix[2], npix[3]), (npix[0], npix[1])), mode='constant', constant_values=cval)
                    erro = np.pad(erro, ((0, 0), (npix[2], npix[3]), (npix[0], npix[1])), mode='constant', constant_values=cval)
                    pxdq = np.pad(pxdq, ((0, 0), (npix[2], npix[3]), (npix[0], npix[1])), mode='constant', constant_values=0)
                    if mask is not None:
                        mask = np.pad(mask, ((npix[2], npix[3]), (npix[0], npix[1])), mode='constant', constant_values=np.nan)
                    crpix1 += npix[0]
                    crpix2 += npix[2]
                    log.info('  --> Frame padding: old shape = ' + str(sh[1:]) + ', new shape = ' + str(data.shape[1:]) + ', fill value = %.2f' % cval)
                
                # Write FITS file and PSF mask.
                head_sci['CRPIX1'] = crpix1
                head_sci['CRPIX2'] = crpix2
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                self.database.update_obs(key, j, fitsfile, maskfile, crpix1=crpix1, crpix2=crpix2)
        
        pass
    
    def coadd_frames(self,
                     nframes=None,
                     types=['SCI', 'SCI_BG', 'REF', 'REF_BG'],
                     subdir='coadded'):
        """
        Coadd frames.
        
        Parameters
        ----------
        nframes : int, optional
            Number of frames to be coadded. Modulo frames will be removed. If
            None, will coadd all frames in an observation. The default is None.
        types : list of str, optional
            List of data types from which the frames shall be coadded. The
            default is ['SCI', 'SCI_BG', 'REF', 'REF_BG'].
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'coadded'.
        
        Returns
        -------
        None.
        
        """
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # The starting value.
        nframes0 = nframes
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            nfitsfiles = len(self.database.obs[key])
            for j in range(nfitsfiles):
                
                # Read FITS file and PSF mask.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                nints = self.database.obs[key]['NINTS'][j]
                effinttm = self.database.obs[key]['EFFINTTM'][j]
                
                # If nframes is not provided, collapse everything.
                if nframes0 is None:
                    nframes = nints
                
                # Skip file types that are not in the list of types.
                if self.database.obs[key]['TYPE'][j] in types:
                    
                    # Coadd frames.
                    head, tail = os.path.split(fitsfile)
                    log.info('  --> Frame coadding: ' + tail)
                    ncoadds = data.shape[0] // nframes
                    data = np.nanmedian(data[:nframes * ncoadds].reshape((nframes, ncoadds, data.shape[-2], data.shape[-1])), axis=0)
                    erro_reshape = erro[:nframes * ncoadds].reshape((nframes, ncoadds, erro.shape[-2], erro.shape[-1]))
                    nsample = np.sum(np.logical_not(np.isnan(erro_reshape)), axis=0)
                    erro = np.true_divide(np.sqrt(np.nansum(erro_reshape**2, axis=0)), nsample)
                    pxdq_temp = pxdq[:nframes * ncoadds].reshape((nframes, ncoadds, pxdq.shape[-2], pxdq.shape[-1]))
                    pxdq = pxdq_temp[0]
                    for k in range(1, nframes):
                        pxdq = np.bitwise_or(pxdq, pxdq_temp[k])
                    if imshifts is not None:
                        imshifts = np.mean(imshifts[:nframes * ncoadds].reshape((nframes, ncoadds, imshifts.shape[-1])), axis=0)
                    if maskoffs is not None:
                        maskoffs = np.mean(maskoffs[:nframes * ncoadds].reshape((nframes, ncoadds, maskoffs.shape[-1])), axis=0)
                    nints = data.shape[0]
                    effinttm *= nframes
                    log.info('  --> Frame coadding: %.0f coadd(s) of %.0f frame(s)' % (ncoadds, nframes))
                
                # Write FITS file and PSF mask.
                head_pri['NINTS'] = nints
                head_pri['EFFINTTM'] = effinttm
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                self.database.update_obs(key, j, fitsfile, maskfile, nints=nints, effinttm=effinttm)
        
        pass
    
    def subtract_median(self,
                        types=['SCI', 'SCI_TA', 'SCI_BG', 'REF', 'REF_TA', 'REF_BG'],
                        subdir='medsub'):
        
        """
        Subtract the median from each frame. Clip everything brighter than 5-
        sigma from the background before computing the median.
        
        Parameters
        ----------
        types : list of str, optional
            List of data types for which the median shall be subtracted. The
            default is ['SCI', 'SCI_TA', 'SCI_BG', 'REF', 'REF_TA', 'REF_BG'].
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'medsub'.
        
        Returns
        -------
        None.
        
        """
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            nfitsfiles = len(self.database.obs[key])
            for j in range(nfitsfiles):
                
                # Read FITS file and PSF mask.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                
                # Skip file types that are not in the list of types.
                if self.database.obs[key]['TYPE'][j] in types:
                    
                    # Subtract median.
                    head, tail = os.path.split(fitsfile)
                    log.info('  --> Median subtraction: ' + tail)
                    data_temp = data.copy()
                    # if self.database.obs[key]['TELESCOP'][j] == 'JWST' and self.database.obs[key]['INSTRUME'][j] == 'NIRCAM':
                    # data_temp[pxdq != 0] = np.nan
                    data_temp[pxdq & 1 == 1] = np.nan
                    # else:
                    #     data_temp[pxdq & 1 == 1] = np.nan
                    bg_med = np.nanmedian(data_temp, axis=(1, 2), keepdims=True)
                    bg_std = robust.medabsdev(data_temp, axis=(1, 2), keepdims=True)
                    bg_ind = data_temp > (bg_med + 5. * bg_std)  # clip bright PSFs for final calculation
                    data_temp[bg_ind] = np.nan
                    bg_med = np.nanmedian(data_temp, axis=(1, 2), keepdims=True)
                    data -= bg_med
                    log.info('  --> Median subtraction: mean of frame median = %.2f' % np.mean(bg_med))
                
                # Write FITS file and PSF mask.
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                self.database.update_obs(key, j, fitsfile, maskfile)
    
    def subtract_background(self,
                            nsplit=1,
                            subdir='bgsub'):
        """
        Subtract the corresponding background observations from the SCI and REF
        data in the spaceKLIP database.
        
        Parameters
        ----------
        nsplit : int, optional
            Number of separate groups into which the SCI/REF and BG data shall
            be split before performing the background subtraction. The default
            is 1.
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'bgsub'.
        
        Returns
        -------
        None.
        
        """
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Find science and reference files.
            ww_sci = np.where(self.database.obs[key]['TYPE'] == 'SCI')[0]
            ww_sci_bg = np.where(self.database.obs[key]['TYPE'] == 'SCI_BG')[0]
            ww_ref = np.where(self.database.obs[key]['TYPE'] == 'REF')[0]
            ww_ref_bg = np.where(self.database.obs[key]['TYPE'] == 'REF_BG')[0]
            
            # Loop through science background files.
            if len(ww_sci_bg) != 0:
                sci_bg_data = []
                sci_bg_erro = []
                sci_bg_pxdq = []
                for j in ww_sci_bg:
                    
                    # Read science background file.
                    fitsfile = self.database.obs[key]['FITSFILE'][j]
                    data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                    
                    # Compute median science background.
                    sci_bg_data += [data]
                    sci_bg_erro += [erro]
                    sci_bg_pxdq += [pxdq]
                sci_bg_data = np.concatenate(sci_bg_data)
                sci_bg_erro = np.concatenate(sci_bg_erro)
                sci_bg_pxdq = np.concatenate(sci_bg_pxdq)
                sci_bg_data_split = np.array_split(sci_bg_data, nsplit, axis=0)
                sci_bg_erro_split = np.array_split(sci_bg_erro, nsplit, axis=0)
                sci_bg_pxdq_split = np.array_split(sci_bg_pxdq, nsplit, axis=0)
                for k in range(nsplit):
                    sci_bg_data_split[k] = np.nanmedian(sci_bg_data_split[k], axis=0)
                    nsample = np.sum(np.logical_not(np.isnan(sci_bg_erro_split[k])), axis=0)
                    sci_bg_erro_split[k] = np.true_divide(np.sqrt(np.nansum(sci_bg_erro_split[k]**2, axis=0)), nsample)
                    sci_bg_pxdq_split[k] = np.sum(sci_bg_pxdq_split[k] & 1 == 1, axis=0) != 0
            else:
                sci_bg_data = None
            
            # Loop through reference background files.
            if len(ww_ref_bg) != 0:
                ref_bg_data = []
                ref_bg_erro = []
                ref_bg_pxdq = []
                for j in ww_ref_bg:
                    
                    # Read reference background file.
                    fitsfile = self.database.obs[key]['FITSFILE'][j]
                    data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                    
                    # Compute median reference background.
                    ref_bg_data += [data]
                    ref_bg_erro += [erro]
                    ref_bg_pxdq += [pxdq]
                ref_bg_data = np.concatenate(ref_bg_data)
                ref_bg_erro = np.concatenate(ref_bg_erro)
                ref_bg_pxdq = np.concatenate(ref_bg_pxdq)
                ref_bg_data_split = np.array_split(ref_bg_data, nsplit, axis=0)
                ref_bg_erro_split = np.array_split(ref_bg_erro, nsplit, axis=0)
                ref_bg_pxdq_split = np.array_split(ref_bg_pxdq, nsplit, axis=0)
                for k in range(nsplit):
                    ref_bg_data_split[k] = np.nanmedian(ref_bg_data_split[k], axis=0)
                    nsample = np.sum(np.logical_not(np.isnan(ref_bg_erro_split[k])), axis=0)
                    ref_bg_erro_split[k] = np.true_divide(np.sqrt(np.nansum(ref_bg_erro_split[k]**2, axis=0)), nsample)
                    ref_bg_pxdq_split[k] = np.sum(ref_bg_pxdq_split[k] & 1 == 1, axis=0) != 0
            else:
                ref_bg_data = None
            
            # Check input.
            if sci_bg_data is None and ref_bg_data is None:
                raise UserWarning('Could not find any background files')
            
            # Loop through science and reference files.
            for j in np.append(ww_sci, ww_ref):
                if j in ww_sci:
                    sci = True
                else:
                    sci = False
                
                # Read FITS file and PSF mask.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                
                # Subtract background.
                head, tail = os.path.split(fitsfile)
                log.info('  --> Background subtraction: ' + tail)
                if sci and sci_bg_data is not None:
                    
                    # test = []
                    # for k in np.logspace(-0.5, 0.5, 100):
                    #     temp = data[0] - k * sci_bg_data
                    #     test += [temp]
                    # test = np.array(test)
                    # hdu0 = pyfits.PrimaryHDU(test)
                    # hdul = pyfits.HDUList([hdu0])
                    # hdul.writeto(os.path.join(output_dir, tail[:-5] + '_test.fits'), output_verify='fix', overwrite=True)
                    # hdul.close()
                    
                    data_split = np.array_split(data, nsplit, axis=0)
                    erro_split = np.array_split(erro, nsplit, axis=0)
                    pxdq_split = np.array_split(pxdq, nsplit, axis=0)
                    for k in range(nsplit):
                        data_split[k] = data_split[k] - sci_bg_data_split[k]
                        erro_split[k] = np.sqrt(erro_split[k]**2 + sci_bg_erro_split[k]**2)
                        pxdq_split[k][np.logical_not(pxdq_split[k] & 1 == 1) & (sci_bg_pxdq_split[k] != 0)] += 1
                    data = np.concatenate(data_split, axis=0)
                    erro = np.concatenate(erro_split, axis=0)
                    pxdq = np.concatenate(pxdq_split, axis=0)
                elif sci and sci_bg_data is None:
                    log.warning('  --> Could not find science background, attempting to use reference background')
                    data_split = np.array_split(data, nsplit, axis=0)
                    erro_split = np.array_split(erro, nsplit, axis=0)
                    pxdq_split = np.array_split(pxdq, nsplit, axis=0)
                    for k in range(nsplit):
                        data_split[k] = data_split[k] - ref_bg_data_split[k]
                        erro_split[k] = np.sqrt(erro_split[k]**2 + ref_bg_erro_split[k]**2)
                        pxdq_split[k][np.logical_not(pxdq_split[k] & 1 == 1) & (ref_bg_pxdq_split[k] != 0)] += 1
                    data = np.concatenate(data_split, axis=0)
                    erro = np.concatenate(erro_split, axis=0)
                    pxdq = np.concatenate(pxdq_split, axis=0)
                elif not sci and ref_bg_data is not None:
                    
                    # test = []
                    # for k in np.logspace(-0.5, 0.5, 100):
                    #     temp = data[0] - k * ref_bg_data
                    #     test += [temp]
                    # test = np.array(test)
                    # hdu0 = pyfits.PrimaryHDU(test)
                    # hdul = pyfits.HDUList([hdu0])
                    # hdul.writeto(os.path.join(output_dir, tail[:-5] + '_test.fits'), output_verify='fix', overwrite=True)
                    # hdul.close()
                    
                    data_split = np.array_split(data, nsplit, axis=0)
                    erro_split = np.array_split(erro, nsplit, axis=0)
                    pxdq_split = np.array_split(pxdq, nsplit, axis=0)
                    for k in range(nsplit):
                        data_split[k] = data_split[k] - ref_bg_data_split[k]
                        erro_split[k] = np.sqrt(erro_split[k]**2 + ref_bg_erro_split[k]**2)
                        pxdq_split[k][np.logical_not(pxdq_split[k] & 1 == 1) & (ref_bg_pxdq_split[k] != 0)] += 1
                    data = np.concatenate(data_split, axis=0)
                    erro = np.concatenate(erro_split, axis=0)
                    pxdq = np.concatenate(pxdq_split, axis=0)
                elif not sci and ref_bg_data is None:
                    log.warning('  --> Could not find reference background, attempting to use science background')
                    data_split = np.array_split(data, nsplit, axis=0)
                    erro_split = np.array_split(erro, nsplit, axis=0)
                    pxdq_split = np.array_split(pxdq, nsplit, axis=0)
                    for k in range(nsplit):
                        data_split[k] = data_split[k] - sci_bg_data_split[k]
                        erro_split[k] = np.sqrt(erro_split[k]**2 + sci_bg_erro_split[k]**2)
                        pxdq_split[k][np.logical_not(pxdq_split[k] & 1 == 1) & (sci_bg_pxdq_split[k] != 0)] += 1
                    data = np.concatenate(data_split, axis=0)
                    erro = np.concatenate(erro_split, axis=0)
                    pxdq = np.concatenate(pxdq_split, axis=0)
                
                # Write FITS file and PSF mask.
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                self.database.update_obs(key, j, fitsfile, maskfile)
        
        pass
    
    def fix_bad_pixels(self,
                       method='timemed+dqmed+medfilt',
                       bpclean_kwargs={},
                       custom_kwargs={},
                       timemed_kwargs={},
                       dqmed_kwargs={},
                       medfilt_kwargs={},
                       types=['SCI', 'SCI_TA', 'SCI_BG', 'REF', 'REF_TA', 'REF_BG'],
                       subdir='bpcleaned'):
        """
        Identify and fix bad pixels.
        
        Parameters
        ----------
        method : str, optional
            Sequence of bad pixel identification and cleaning methods to be run
            on the data. Different methods must be joined by a '+' sign without
            whitespace. Available methods are:
            - bpclean: use sigma clipping to identify additional bad pixels.
            - custom: use a custom bad pixel map.
            - timemed: replace pixels which are only bad in some frames with
                       their median value from the good frames.
            - dqmed: replace bad pixels with the median value of their
                     surrounding good pixels.
            - medfilt: replace bad pixels with an image plane median filter.
            The default is 'timemed+dqmed+medfilt'.
        bpclean_kwargs : dict, optional
            Keyword arguments for the 'bpclean' method. Available keywords are:
            - sigclip : float, optional
                Sigma clipping threshold. The default is 5.
            - shift_x : list of int, optional
                Pixels in x-direction to which each pixel shall be compared to.
                The default is [-1, 0, 1].
            - shift_y : list of int, optional
                Pixels in y-direction to which each pixel shall be compared to.
                The default is [-1, 0, 1].
            The default is {}.
        custom_kwargs : dict, optional
            Keyword arguments for the 'custom' method. The dictionary keys must
            match the keys of the observations database and the dictionary
            content must be binary bad pixel maps (1 = bad, 0 = good) with the
            same shape as the corresponding data. The default is {}.
        timemed_kwargs : dict, optional
            Keyword arguments for the 'timemed' method. Available keywords are:
            - n/a
            The default is {}.
        dqmed_kwargs : dict, optional
            Keyword arguments for the 'dqmed' method. Available keywords are:
            - shift_x : list of int, optional
                Pixels in x-direction from which the median shall be computed.
                The default is [-1, 0, 1].
            - shift_y : list of int, optional
                Pixels in y-direction from which the median shall be computed.
                The default is [-1, 0, 1].
            The default is {}.
        medfilt_kwargs : dict, optional
            Keyword arguments for the 'medfilt' method. Available keywords are:
            - size : int, optional
                Kernel size of the median filter to be used. The default is 4.
            The default is {}.
        types : list of str, optional
            List of data types for which bad pixels shall be identified and
            fixed. The default is ['SCI', 'SCI_TA', 'SCI_BG', 'REF', 'REF_TA',
            'REF_BG'].
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'bpcleaned'.
        
        Returns
        -------
        None.
        
        """
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            nfitsfiles = len(self.database.obs[key])
            for j in range(nfitsfiles):
                
                # Read FITS file and PSF mask.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                
                # Skip file types that are not in the list of types.
                if self.database.obs[key]['TYPE'][j] in types:
                    
                    # Call bad pixel cleaning routines.
                    pxdq_temp = pxdq.copy()
                    # if self.database.obs[key]['TELESCOP'][j] == 'JWST' and self.database.obs[key]['INSTRUME'][j] == 'NIRCAM':
                    #     pxdq_temp = (pxdq_temp != 0) & np.logical_not(pxdq_temp & 512 == 512)
                    # else:
                    pxdq_temp = (np.isnan(data) | (pxdq_temp & 1 == 1)) & np.logical_not(pxdq_temp & 512 == 512)
                    method_split = method.split('+')
                    for k in range(len(method_split)):
                        head, tail = os.path.split(fitsfile)
                        if method_split[k] == 'bpclean':
                            log.info('  --> Method ' + method_split[k] + ': ' + tail)
                            self.find_bad_pixels_bpclean(data, erro, pxdq_temp, pxdq & 512 == 512, bpclean_kwargs)
                        elif method_split[k] == 'custom':
                            log.info('  --> Method ' + method_split[k] + ': ' + tail)
                            if self.database.obs[key]['TYPE'][j] not in ['SCI_TA', 'REF_TA']:
                                self.find_bad_pixels_custom(data, erro, pxdq_temp, key, custom_kwargs)
                            else:
                                log.info('  --> Method ' + method_split[k] + ': skipped because TA file')
                        elif method_split[k] == 'timemed':
                            log.info('  --> Method ' + method_split[k] + ': ' + tail)
                            self.fix_bad_pixels_timemed(data, erro, pxdq_temp, timemed_kwargs)
                        elif method_split[k] == 'dqmed':
                            log.info('  --> Method ' + method_split[k] + ': ' + tail)
                            self.fix_bad_pixels_dqmed(data, erro, pxdq_temp, dqmed_kwargs)
                        elif method_split[k] == 'medfilt':
                            log.info('  --> Method ' + method_split[k] + ': ' + tail)
                            self.fix_bad_pixels_medfilt(data, erro, pxdq_temp, medfilt_kwargs)
                        else:
                            log.info('  --> Unknown method ' + method_split[k] + ': skipped')
                    # if self.database.obs[key]['TELESCOP'][j] == 'JWST' and self.database.obs[key]['INSTRUME'][j] == 'NIRCAM':
                    #     pxdq[(pxdq != 0) & np.logical_not(pxdq & 512 == 512) & (pxdq_temp == 0)] = 0
                    # else:
                    # pxdq[(pxdq & 1 == 1) & np.logical_not(pxdq & 512 == 512) & (pxdq_temp == 0)] = 0
                
                # Write FITS file and PSF mask.
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                self.database.update_obs(key, j, fitsfile, maskfile)
        
        pass
    
    def find_bad_pixels_bpclean(self,
                                data,
                                erro,
                                pxdq,
                                NON_SCIENCE,
                                bpclean_kwargs={}):
        """
        Use an iterative sigma clipping algorithm to identify additional bad
        pixels in the data.
        
        Parameters
        ----------
        data : 3D-array
            Input images.
        erro : 3D-array
            Input image uncertainties.
        pxdq : 3D-array
            Input binary bad pixel maps (1 = bad, 0 = good). Will be updated by
            the routine to include the newly identified bad pixels.
        NON_SCIENCE : 3D-array
            Input binary non-science pixel maps (1 = bad, 0 = good). Will not
            be modified by the routine.
        bpclean_kwargs : dict, optional
            Keyword arguments for the 'bpclean' method. Available keywords are:
            - sigclip : float, optional
                Sigma clipping threshold. The default is 5.
            - shift_x : list of int, optional
                Pixels in x-direction to which each pixel shall be compared to.
                The default is [-1, 0, 1].
            - shift_y : list of int, optional
                Pixels in y-direction to which each pixel shall be compared to.
                The default is [-1, 0, 1].
            The default is {}.
        
        Returns
        -------
        None.
        
        """
        
        # Check input.
        if 'sigclip' not in bpclean_kwargs.keys():
            bpclean_kwargs['sigclip'] = 5.
        if 'shift_x' not in bpclean_kwargs.keys():
            bpclean_kwargs['shift_x'] = [-1, 0, 1]
        if 'shift_y' not in bpclean_kwargs.keys():
            bpclean_kwargs['shift_y'] = [-1, 0, 1]
        if 0 not in bpclean_kwargs['shift_x']:
            bpclean_kwargs['shift_x'] += [0]
        if 0 not in bpclean_kwargs['shift_y']:
            bpclean_kwargs['shift_y'] += [0]
        
        # Pad data.
        pad_left = np.abs(np.min(bpclean_kwargs['shift_x']))
        pad_right = np.abs(np.max(bpclean_kwargs['shift_x']))
        if pad_right == 0:
            right = None
        else:
            right = -pad_right
        pad_bottom = np.abs(np.min(bpclean_kwargs['shift_y']))
        pad_top = np.abs(np.max(bpclean_kwargs['shift_y']))
        if pad_top == 0:
            top = None
        else:
            top = -pad_top
        pad_vals = ((pad_bottom, pad_top), (pad_left, pad_right))
        
        # Find bad pixels using median of neighbors.
        pxdq_orig = pxdq.copy()
        ww = pxdq != 0
        data_temp = data.copy()
        data_temp[ww] = np.nan
        erro_temp = erro.copy()
        erro_temp[ww] = np.nan
        for i in range(ww.shape[0]):
            
            # Get median background and standard deviation.
            bg_med = np.nanmedian(data_temp[i])
            bg_std = robust.medabsdev(data_temp[i])
            bg_ind = data[i] < (bg_med + 10. * bg_std)  # clip bright PSFs for final calculation
            bg_med = np.nanmedian(data_temp[i][bg_ind])
            bg_std = robust.medabsdev(data_temp[i][bg_ind])
            
            # Create initial mask of large negative values.
            ww[i] = ww[i] | (data[i] < bg_med - bpclean_kwargs['sigclip'] * bg_std)
            
            # Loop through max 10 iterations.
            for it in range(10):
                data_temp[i][ww[i]] = np.nan
                erro_temp[i][ww[i]] = np.nan
                
                # Shift data. 
                pad_data = np.pad(data_temp[i], pad_vals, mode='edge')
                pad_erro = np.pad(erro_temp[i], pad_vals, mode='edge')
                data_arr = []
                erro_arr = []
                for ix in bpclean_kwargs['shift_x']:
                    for iy in bpclean_kwargs['shift_y']:
                        if ix != 0 or iy != 0:
                            data_arr += [np.roll(pad_data, (iy, ix), axis=(0, 1))]
                            erro_arr += [np.roll(pad_erro, (iy, ix), axis=(0, 1))]
                data_arr = np.array(data_arr)
                data_arr = data_arr[:, pad_bottom:top, pad_left:right]
                data_med = np.nanmedian(data_arr, axis=0)
                diff = data[i] - data_med
                data_std = np.nanstd(data_arr, axis=0)
                # data_std = robust.medabsdev(data_arr, axis=0)
                mask_new = diff > bpclean_kwargs['sigclip'] * data_std
                nmask_new = np.sum(mask_new & np.logical_not(ww[i]))
                # print('Iteration %.0f: %.0f bad pixels identified, %.0f are new' % (it + 1, np.sum(mask_new), nmask_new))
                sys.stdout.write('\rFrame %.0f/%.0f, iteration %.0f' % (i + 1, ww.shape[0], it + 1))
                sys.stdout.flush()
                if it > 0 and nmask_new == 0:
                    break
                ww[i] = ww[i] | mask_new
            ww[i][NON_SCIENCE[i]] = 0
            pxdq[i][ww[i]] = 1
        print('')
        log.info('  --> Method bpclean: identified %.0f additional bad pixel(s) -- %.2f%%' % (np.sum(pxdq) - np.sum(pxdq_orig), 100. * (np.sum(pxdq) - np.sum(pxdq_orig)) / np.prod(pxdq.shape)))
        
        pass
    
    def find_bad_pixels_custom(self,
                               data,
                               erro,
                               pxdq,
                               key,
                               custom_kwargs={}):
        """
        Use a custom bad pixel map to flag additional bad pixels in the data.
        
        Parameters
        ----------
        data : 3D-array
            Input images.
        erro : 3D-array
            Input image uncertainties.
        pxdq : 3D-array
            Input binary bad pixel maps (1 = bad, 0 = good). Will be updated by
            the routine to include the newly flagged bad pixels.
        key : str
            Database key of the observation to be updated.
        custom_kwargs : dict, optional
            Keyword arguments for the 'custom' method. The dictionary keys must
            match the keys of the observations database and the dictionary
            content must be binary bad pixel maps (1 = bad, 0 = good) with the
            same shape as the corresponding data. The default is {}.
        
        Returns
        -------
        None.
        
        """
        
        # Find bad pixels using median of neighbors.
        pxdq_orig = pxdq.copy()
        pxdq_custom = custom_kwargs[key] != 0
        pxdq_custom = np.array([pxdq_custom] * pxdq.shape[0])
        pxdq[pxdq_custom] = 1
        log.info('  --> Method custom: flagged %.0f additional bad pixel(s) -- %.2f%%' % (np.sum(pxdq) - np.sum(pxdq_orig), 100. * (np.sum(pxdq) - np.sum(pxdq_orig)) / np.prod(pxdq.shape)))
        
        pass
    
    def fix_bad_pixels_timemed(self,
                               data,
                               erro,
                               pxdq,
                               timemed_kwargs={}):
        """
        Replace pixels which are only bad in some frames with their median
        value from the good frames.
        
        Parameters
        ----------
        data : 3D-array
            Input images.
        erro : 3D-array
            Input image uncertainties.
        pxdq : 3D-array
            Input binary bad pixel maps (1 = bad, 0 = good). Will be updated by
            the routine to exclude the fixed bad pixels.
        timemed_kwargs : dict, optional
            Keyword arguments for the 'timemed' method. Available keywords are:
            - n/a
            The default is {}.
        
        Returns
        -------
        None.
        
        """
        
        # Fix bad pixels using time median.
        ww = pxdq != 0
        ww_all_bad = np.array([np.sum(ww, axis=0) == ww.shape[0]] * ww.shape[0])
        ww_not_all_bad = ww & np.logical_not(ww_all_bad)
        log.info('  --> Method timemed: fixing %.0f bad pixel(s) -- %.2f%%' % (np.sum(ww_not_all_bad), 100. * np.sum(ww_not_all_bad) / np.prod(ww_not_all_bad.shape)))
        data[ww_not_all_bad] = np.nan
        data[ww_not_all_bad] = np.array([np.nanmedian(data, axis=0)] * data.shape[0])[ww_not_all_bad]
        erro[ww_not_all_bad] = np.nan
        erro[ww_not_all_bad] = np.array([np.nanmedian(erro, axis=0)] * erro.shape[0])[ww_not_all_bad]
        pxdq[ww_not_all_bad] = 0
        
        pass
    
    def fix_bad_pixels_dqmed(self,
                             data,
                             erro,
                             pxdq,
                             dqmed_kwargs={}):
        """
        Replace bad pixels with the median value of their surrounding good
        pixels.
        
        Parameters
        ----------
        data : 3D-array
            Input images.
        erro : 3D-array
            Input image uncertainties.
        pxdq : 3D-array
            Input binary bad pixel maps (1 = bad, 0 = good). Will be updated by
            the routine to exclude the fixed bad pixels.
        dqmed_kwargs : dict, optional
            Keyword arguments for the 'dqmed' method. Available keywords are:
            - shift_x : list of int, optional
                Pixels in x-direction from which the median shall be computed.
                The default is [-1, 0, 1].
            - shift_y : list of int, optional
                Pixels in y-direction from which the median shall be computed.
                The default is [-1, 0, 1].
            The default is {}.
        
        Returns
        -------
        None.
        
        """
        
        # Check input.
        if 'shift_x' not in dqmed_kwargs.keys():
            dqmed_kwargs['shift_x'] = [-1, 0, 1]
        if 'shift_y' not in dqmed_kwargs.keys():
            dqmed_kwargs['shift_y'] = [-1, 0, 1]
        if 0 not in dqmed_kwargs['shift_x']:
            dqmed_kwargs['shift_x'] += [0]
        if 0 not in dqmed_kwargs['shift_y']:
            dqmed_kwargs['shift_y'] += [0]
        
        # Pad data.
        pad_left = np.abs(np.min(dqmed_kwargs['shift_x']))
        pad_right = np.abs(np.max(dqmed_kwargs['shift_x']))
        if pad_right == 0:
            right = None
        else:
            right = -pad_right
        pad_bottom = np.abs(np.min(dqmed_kwargs['shift_y']))
        pad_top = np.abs(np.max(dqmed_kwargs['shift_y']))
        if pad_top == 0:
            top = None
        else:
            top = -pad_top
        pad_vals = ((0, 0), (pad_bottom, pad_top), (pad_left, pad_right))
        
        # Fix bad pixels using median of neighbors.
        ww = pxdq != 0
        data_temp = data.copy()
        data_temp[ww] = np.nan
        pad_data = np.pad(data_temp, pad_vals, mode='edge')
        erro_temp = erro.copy()
        erro_temp[ww] = np.nan
        pad_erro = np.pad(erro_temp, pad_vals, mode='edge')
        for i in range(ww.shape[0]):
            data_arr = []
            erro_arr = []
            for ix in dqmed_kwargs['shift_x']:
                for iy in dqmed_kwargs['shift_y']:
                    if ix != 0 or iy != 0:
                        data_arr += [np.roll(pad_data[i], (iy, ix), axis=(0, 1))]
                        erro_arr += [np.roll(pad_erro[i], (iy, ix), axis=(0, 1))]
            data_arr = np.array(data_arr)
            data_arr = data_arr[:, pad_bottom:top, pad_left:right]
            data_med = np.nanmedian(data_arr, axis=0)
            ww[i][np.isnan(data_med)] = 0
            data[i][ww[i]] = data_med[ww[i]]
            erro_arr = np.array(erro_arr)
            erro_arr = erro_arr[:, pad_bottom:top, pad_left:right]
            erro_med = np.nanmedian(erro_arr, axis=0)
            erro[i][ww[i]] = erro_med[ww[i]]
            pxdq[i][ww[i]] = 0
        log.info('  --> Method dqmed: fixing %.0f bad pixel(s) -- %.2f%%' % (np.sum(ww), 100. * np.sum(ww) / np.prod(ww.shape)))
        
        pass
    
    def fix_bad_pixels_medfilt(self,
                               data,
                               erro,
                               pxdq,
                               medfilt_kwargs={}):
        """
        Replace bad pixels with an image plane median filter.
        
        Parameters
        ----------
        data : 3D-array
            Input images.
        erro : 3D-array
            Input image uncertainties.
        pxdq : 3D-array
            Input binary bad pixel maps (1 = bad, 0 = good). Will be updated by
            the routine to exclude the fixed bad pixels.
        medfilt_kwargs : dict, optional
            Keyword arguments for the 'medfilt' method. Available keywords are:
            - size : int, optional
                Kernel size of the median filter to be used. The default is 4.
            The default is {}.
        
        Returns
        -------
        None.
        
        """
        
        # Check input.
        if 'size' not in medfilt_kwargs.keys():
            medfilt_kwargs['size'] = 4
        
        # Fix bad pixels using median filter.
        ww = pxdq != 0
        log.info('  --> Method medfilt: fixing %.0f bad pixel(s) -- %.2f%%' % (np.sum(ww), 100. * np.sum(ww) / np.prod(ww.shape)))
        data_temp = data.copy()
        data_temp[np.isnan(data_temp)] = 0.
        erro_temp = erro.copy()
        erro_temp[np.isnan(erro_temp)] = 0.
        for i in range(ww.shape[0]):
            data[i][ww[i]] = median_filter(data_temp[i], **medfilt_kwargs)[ww[i]]
            erro[i][ww[i]] = median_filter(erro_temp[i], **medfilt_kwargs)[ww[i]]
            pxdq[i][ww[i]] = 0
        
        pass
    
    def replace_nans(self,
                     cval=0.,
                     types=['SCI', 'SCI_BG', 'REF', 'REF_BG'],
                     subdir='nanreplaced'):
        """
        Replace all nans in the data with a constant value.
        
        Parameters
        ----------
        cval : float, optional
            Fill value for the nan pixels. The default is 0.
        types : list of str, optional
            List of data types for which nans shall be replaced. The default is
            ['SCI', 'SCI_BG', 'REF', 'REF_BG'].
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'nanreplaced'.
        
        Returns
        -------
        None.
        
        """
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            nfitsfiles = len(self.database.obs[key])
            for j in range(nfitsfiles):
                
                # Read FITS file and PSF mask.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                
                # Skip file types that are not in the list of types.
                if self.database.obs[key]['TYPE'][j] in types:
                    
                    # Replace nans.
                    head, tail = os.path.split(fitsfile)
                    log.info('  --> Nan replacement: ' + tail)
                    ww = np.isnan(data)
                    data[ww] = cval
                    log.info('  --> Nan replacement: replaced %.0f nan pixel(s) with value ' % (np.sum(ww)) + str(cval) + ' -- %.2f%%' % (100. * np.sum(ww)/np.prod(ww.shape)))
                
                # Write FITS file and PSF mask.
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                self.database.update_obs(key, j, fitsfile, maskfile)
        
        pass
    
    def blur_frames(self,
                    fact='auto',
                    types=['SCI', 'SCI_BG', 'REF', 'REF_BG'],
                    subdir='blurred'):
        """
        Blur frames with a Gaussian filter.
        
        Parameters
        ----------
        fact : 'auto' or float or dict of list of float or None, optional
            FWHM (pix) of the Gaussian filter. If 'auto', will compute the FWHM
            automatically based on the Nyquist sampling criterion for discrete
            data, which is FWHM = lambda / 2.3D, where D = 5.2 m for NIRCam
            coronagraphy and D = 6.5 m otherwise. If dict of list of float,
            then the dictionary keys must match the keys of the observations
            database, and the number of entries in the lists must match the
            number of observations in the corresponding concatenation. Then, a
            different FWHM can be used for each observation. If None, the
            corresponding observation will be skipped. The default is 'auto'.
        types : list of str, optional
            List of data types for which the frames shall be blurred. The
            default is ['SCI', 'SCI_BG', 'REF', 'REF_BG'].
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'blurred'.
        
        Returns
        -------
        None.
        
        """
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            Nfitsfiles = len(self.database.obs[key])
            for j in range(Nfitsfiles):
                
                # Read FITS file.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                
                # Skip file types that are not in the list of types.
                fact_temp = None
                if self.database.obs[key]['TYPE'][j] in types:
                    
                    # Blur frames.
                    head, tail = os.path.split(fitsfile)
                    log.info('  --> Frame blurring: ' + tail)
                    try:
                        fact_temp = fact[key][j]
                    except:
                        fact_temp = fact
                    if self.database.obs[key]['TELESCOP'][j] == 'JWST':
                        if self.database.obs[key]['EXP_TYPE'][j] in ['NRC_CORON']:
                            diam = 5.2
                        else:
                            diam = 6.5
                    else:
                        raise UserWarning('Data originates from unknown telescope')
                    if fact_temp is not None:
                        if str(fact_temp) == 'auto':
                            wave_min = self.database.obs[key]['CWAVEL'][j] - self.database.obs[key]['DWAVEL'][j]
                            nyquist = wave_min * 1e-6 / diam * 180. / np.pi * 3600. * 1000. / 2.3  # see, e.g., Pawley 2006
                            fact_temp = self.database.obs[key]['PIXSCALE'][j] / nyquist
                            fact_temp /= np.sqrt(8. * np.log(2.))  # fix from Marshall
                        log.info('  --> Frame blurring: factor = %.3f' % fact_temp)
                        for k in range(data.shape[0]):
                            data[k] = gaussian_filter(data[k], fact_temp)
                            erro[k] = gaussian_filter(erro[k], fact_temp)
                        if mask is not None:
                            mask = gaussian_filter(mask, fact_temp)
                    else:
                        log.info('  --> Frame blurring: skipped')
                
                # Write FITS file.
                if fact_temp is None:
                    pass
                else:
                    head_pri['BLURFWHM'] = fact_temp
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                if fact_temp is None:
                    self.database.update_obs(key, j, fitsfile, maskfile, blurfwhm=np.nan)
                else:
                    self.database.update_obs(key, j, fitsfile, maskfile, blurfwhm=fact_temp)
        
        pass
    
    def hpf(self,
            size='auto',
            types=['SCI', 'SCI_BG', 'REF', 'REF_BG'],
            subdir='filtered'):
        """
        Blur frames with a Gaussian filter.
        
        Parameters
        ----------
        fact : 'auto' or float or dict of list of float or None, optional
            FWHM (pix) of the Gaussian filter. If 'auto', will compute the FWHM
            automatically based on the Nyquist sampling criterion for discrete
            data, which is FWHM = lambda / 2.3D, where D = 5.2 m for NIRCam
            coronagraphy and D = 6.5 m otherwise. If dict of list of float,
            then the dictionary keys must match the keys of the observations
            database, and the number of entries in the lists must match the
            number of observations in the corresponding concatenation. Then, a
            different FWHM can be used for each observation. If None, the
            corresponding observation will be skipped. The default is 'auto'.
        types : list of str, optional
            List of data types for which the frames shall be blurred. The
            default is ['SCI', 'SCI_BG', 'REF', 'REF_BG'].
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'blurred'.
        
        Returns
        -------
        None.
        
        """
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            Nfitsfiles = len(self.database.obs[key])
            for j in range(Nfitsfiles):
                
                # Read FITS file.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                
                # Skip file types that are not in the list of types.
                fact_temp = None
                if self.database.obs[key]['TYPE'][j] in types:
                    
                    # High-pass filter frames.
                    head, tail = os.path.split(fitsfile)
                    log.info('  --> Frame filtering: ' + tail)
                    try:
                        size_temp = size[key][j]
                    except:
                        size_temp = size
                    if size_temp is not None:
                        for k in range(data.shape[0]):
                            orig = data[k].copy()
                            temp = gaussian_filter(data[k], 3.)
                            data[k] = orig - temp
                            # f, ax = plt.subplots(1, 2, figsize=(2 * 6.4, 1 * 4.8))
                            # ax[0].imshow(np.log10(np.abs(orig)), origin='lower')
                            # ax[1].imshow(np.log10(np.abs(data[k])), origin='lower')
                            # plt.tight_layout()
                            # plt.show()
                            # pdb.set_trace()
                            orig = erro[k].copy()
                            temp = gaussian_filter(erro[k], 3.)
                            erro[k] = orig - temp
                    else:
                        log.info('  --> Frame filtering: skipped')
                
                # Write FITS file.
                if size_temp is None:
                    pass
                else:
                    head_pri['HPFSIZE'] = fact_temp
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                self.database.update_obs(key, j, fitsfile, maskfile)
        
        pass
    
    def update_nircam_centers(self):
        """
        Determine offset between SIAF reference pixel position and true mask
        center from Jarron and update the current reference pixel position to
        reflect the true mask center. Account for filter-dependent distortion.
        Might not be required for simulated data.
        
        Returns
        -------
        None.
        
        """
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            for j in range(len(self.database.obs[key])):
                
                # Skip file types that are not NIRCam coronagraphy.
                if self.database.obs[key]['EXP_TYPE'][j] == 'NRC_CORON':
                    
                    # Read FITS file and PSF mask.
                    fitsfile = self.database.obs[key]['FITSFILE'][j]
                    data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                    maskfile = self.database.obs[key]['MASKFILE'][j]
                    mask = ut.read_msk(maskfile)
                    
                    # Update current reference pixel position.
                    head, tail = os.path.split(fitsfile)
                    log.info('  --> Update NIRCam coronagraphy centers: ' + tail)
                    
                    # Get current reference pixel position.
                    crpix1 = self.database.obs[key]['CRPIX1'][j]
                    crpix2 = self.database.obs[key]['CRPIX2'][j]
                    
                    # Get SIAF reference pixel position.
                    siaf = pysiaf.Siaf('NIRCAM')
                    apsiaf = siaf[self.database.obs[key]['APERNAME'][j]]
                    xsciref, ysciref = (apsiaf.XSciRef, apsiaf.YSciRef)
                    
                    # Get true mask center from Jarron.
                    try:
                        crpix1_jarron, crpix2_jarron = crpix_jarron[self.database.obs[key]['APERNAME'][j]]
                    except KeyError:
                        log.warning('  --> Update NIRCam coronagraphy centers: no true mask center found for ' + self.database.obs[key]['APERNAME'][j])
                        crpix1_jarron, crpix2_jarron = xsciref, ysciref
                    
                    # Get filter shift from Jarron.
                    try:
                        xshift_jarron, yshift_jarron = filter_shifts_jarron[self.database.obs[key]['FILTER'][j]]
                    except KeyError:
                        log.warning('  --> Update NIRCam coronagraphy centers: no filter shift found for ' + self.database.obs[key]['FILTER'][j])
                        xshift_jarron, yshift_jarron = 0., 0.
                    
                    # Determine offset between SIAF reference pixel position
                    # and true mask center from Jarron and update current
                    # reference pixel position. Account for filter-dependent
                    # distortion.
                    xoff, yoff = crpix1_jarron + xshift_jarron - xsciref, crpix2_jarron + yshift_jarron - ysciref
                    log.info('  --> Update NIRCam coronagraphy centers: old = (%.2f, %.2f), new = (%.2f, %.2f)' % (crpix1, crpix2, crpix1 + xoff, crpix2 + yoff))
                    crpix1 += xoff
                    crpix2 += yoff
                    
                    # Update spaceKLIP database.
                    self.database.update_obs(key, j, fitsfile, maskfile, crpix1=crpix1, crpix2=crpix2)
        
        pass
    
    def recenter_frames(self,
                        method='fourier',
                        subpix_first_sci_only=False,
                        spectral_type='G2V',
                        kwargs={},
                        subdir='recentered'):
        """
        Recenter frames so that the host star position is data.shape // 2. For
        NIRCam coronagraphy, use a WebbPSF model to determine the star position
        behind the coronagraphic mask for the first SCI frame. Then, shift all
        other SCI and REF frames by the same amount. For MIRI coronagraphy, do
        nothing. For all other data types, simply recenter the host star PSF.
        
        Parameters
        ----------
        method : 'fourier' or 'spline' (not recommended), optional
            Method for shifting the frames. The default is 'fourier'.
        subpix_first_sci_only : bool, optional
            By default, all frames will be recentered to subpixel precision. If
            'subpix_first_sci_only' is True, then only the first SCI frame will
            be recentered to subpixel precision and all other SCI and REF
            frames will only be recentered to integer pixel precision by
            rolling the image. Can be helpful when working with poorly sampled
            data to avoid another interpolation step if the 'align_frames'
            routine is run subsequently. Only applicable to non-coronagraphic
            data. The default is False.
        spectral_type : str, optional
            Host star spectral type for the WebbPSF model used to determine the
            star position behind the coronagraphic mask. The default is 'G2V'.
        kwargs : dict, optional
            Keyword arguments for the scipy.ndimage.shift routine. The default
            is {}.
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'recentered'.
        
        Returns
        -------
        None.
        
        """
        
        # Update NIRCam coronagraphy centers, i.e., change SIAF CRPIX position
        # to true mask center determined by Jarron.
        # self.update_nircam_centers()  # shall be run purposely by the user
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Find science and reference files.
            ww_sci = np.where(self.database.obs[key]['TYPE'] == 'SCI')[0]
            ww_sci_ta = np.where(self.database.obs[key]['TYPE'] == 'SCI_TA')[0]
            ww_ref = np.where(self.database.obs[key]['TYPE'] == 'REF')[0]
            ww_ref_ta = np.where(self.database.obs[key]['TYPE'] == 'REF_TA')[0]
            
            # Loop through FITS files.
            ww_all = np.append(ww_sci, ww_ref)
            ww_all = np.append(ww_all, ww_sci_ta)
            ww_all = np.append(ww_all, ww_ref_ta)
            shifts_all = []
            for j in ww_all:
                
                # Read FITS file and PSF mask.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                
                # Recenter frames. Use different algorithms based on data type.
                head, tail = os.path.split(fitsfile)
                log.info('  --> Recenter frames: ' + tail)
                if np.sum(np.isnan(data)) != 0:
                    raise UserWarning('Please replace nan pixels before attempting to recenter frames')
                shifts = []  # shift between star position and image center (data.shape // 2)
                maskoffs_temp = []  # shift between star and coronagraphic mask position
                
                # SCI and REF data.
                if j in ww_sci or j in ww_ref:
                    
                    # NIRCam coronagraphy.
                    if self.database.obs[key]['EXP_TYPE'][j] in ['NRC_CORON']:
                        for k in range(data.shape[0]):
                            
                            # For the first SCI frame, get the star position
                            # and the shift between the star and coronagraphic
                            # mask position.
                            if j == ww_sci[0] and k == 0:
                                xc, yc, xshift, yshift = self.find_nircam_centers(data0=data[k].copy(),
                                                                                  key=key,
                                                                                  j=j,
                                                                                  spectral_type=spectral_type,
                                                                                  date=head_pri['DATE-BEG'],
                                                                                  output_dir=output_dir)
                            
                            # Apply the same shift to all SCI and REF frames.
                            shifts += [np.array([-(xc - data.shape[-1]//2), -(yc - data.shape[-2]//2)])]
                            maskoffs_temp += [np.array([xshift, yshift])]
                            data[k] = ut.imshift(data[k], [shifts[k][0], shifts[k][1]], method=method, kwargs=kwargs)
                            erro[k] = ut.imshift(erro[k], [shifts[k][0], shifts[k][1]], method=method, kwargs=kwargs)
                        if mask is not None:
                            # mask = ut.imshift(mask, [shifts[k][0], shifts[k][1]], method=method, kwargs=kwargs)
                            mask = spline_shift(mask, [shifts[k][1], shifts[k][0]], order=0, mode='constant', cval=np.nanmedian(mask))
                        xoffset = self.database.obs[key]['XOFFSET'][j] - self.database.obs[key]['XOFFSET'][ww_sci[0]]  # mas
                        yoffset = self.database.obs[key]['YOFFSET'][j] - self.database.obs[key]['YOFFSET'][ww_sci[0]]  # mas
                        crpix1 = data.shape[-1]//2 + 1  # 1-indexed
                        crpix2 = data.shape[-2]//2 + 1  # 1-indexed
                    
                    # MIRI coronagraphy.
                    elif self.database.obs[key]['EXP_TYPE'][j] in ['MIR_4QPM', 'MIR_LYOT']:
                        log.warning('  --> Recenter frames: not implemented for MIRI coronagraphy, skipped')
                        for k in range(data.shape[0]):
                            
                            # Do nothing.
                            shifts += [np.array([0., 0.])]
                            maskoffs_temp += [np.array([0., 0.])]
                        xoffset = self.database.obs[key]['XOFFSET'][j]  # mas
                        yoffset = self.database.obs[key]['YOFFSET'][j]  # mas
                        crpix1 = self.database.obs[key]['CRPIX1'][j]  # 1-indexed
                        crpix2 = self.database.obs[key]['CRPIX2'][j]  # 1-indexed
                    
                    # Other data types.
                    else:
                        for k in range(data.shape[0]):
                            
                            # Recenter SCI and REF frames to subpixel precision
                            # using the 'BCEN' routine from XARA.
                            # https://github.com/fmartinache/xara
                            if subpix_first_sci_only == False or (j == ww_sci[0] and k == 0):
                                pp = core.determine_origin(data[k], algo='BCEN')
                                shifts += [np.array([-(pp[0] - data.shape[-1]//2), -(pp[1] - data.shape[-2]//2)])]
                                maskoffs_temp += [np.array([0., 0.])]
                                data[k] = ut.imshift(data[k], [shifts[k][0], shifts[k][1]], method=method, kwargs=kwargs)
                                erro[k] = ut.imshift(erro[k], [shifts[k][0], shifts[k][1]], method=method, kwargs=kwargs)
                            else:
                                shifts += [np.array([0., 0.])]
                                maskoffs_temp += [np.array([0., 0.])]
                            
                            # Recenter SCI and REF frames to integer pixel
                            # precision by rolling the image.
                            ww_max = np.unravel_index(np.argmax(data[k]), data[k].shape)
                            if ww_max != (data.shape[-2]//2, data.shape[-1]//2):
                                dx, dy = data.shape[-1]//2 - ww_max[1], data.shape[-2]//2 - ww_max[0]
                                shifts[-1][0] += dx
                                shifts[-1][1] += dy
                                data[k] = np.roll(np.roll(data[k], dx, axis=1), dy, axis=0)
                                erro[k] = np.roll(np.roll(erro[k], dx, axis=1), dy, axis=0)
                        xoffset = 0.  # mas
                        yoffset = 0.  # mas
                        crpix1 = data.shape[-1]//2 + 1  # 1-indexed
                        crpix2 = data.shape[-2]//2 + 1  # 1-indexed
                
                # TA data.
                if j in ww_sci_ta or j in ww_ref_ta:
                    for k in range(data.shape[0]):
                        
                        # Center TA frames on the nearest pixel center. This
                        # pixel center is not necessarily the image center,
                        # which is why a subsequent integer pixel recentering
                        # is required.
                        p0 = np.array([0., 0.])
                        pp = minimize(ut.recenterlsq,
                                      p0,
                                      args=(data[k], method, kwargs))['x']
                        shifts += [np.array([pp[0], pp[1]])]
                        maskoffs_temp += [np.array([0., 0.])]
                        data[k] = ut.imshift(data[k], [shifts[k][0], shifts[k][1]], method=method, kwargs=kwargs)
                        erro[k] = ut.imshift(erro[k], [shifts[k][0], shifts[k][1]], method=method, kwargs=kwargs)
                        
                        # Recenter TA frames to integer pixel precision by
                        # rolling the image.
                        ww_max = np.unravel_index(np.argmax(data[k]), data[k].shape)
                        if ww_max != (data.shape[-2]//2, data.shape[-1]//2):
                            dx, dy = data.shape[-1]//2 - ww_max[1], data.shape[-2]//2 - ww_max[0]
                            shifts[-1][0] += dx
                            shifts[-1][1] += dy
                            data[k] = np.roll(np.roll(data[k], dx, axis=1), dy, axis=0)
                            erro[k] = np.roll(np.roll(erro[k], dx, axis=1), dy, axis=0)
                    xoffset = 0.  # mas
                    yoffset = 0.  # mas
                    crpix1 = data.shape[-1]//2 + 1  # 1-indexed
                    crpix2 = data.shape[-2]//2 + 1  # 1-indexed
                shifts = np.array(shifts)
                shifts_all += [shifts]
                maskoffs_temp = np.array(maskoffs_temp)
                if imshifts is not None:
                    imshifts += shifts
                else:
                    imshifts = shifts
                if maskoffs is not None:
                    maskoffs += maskoffs_temp
                else:
                    maskoffs = maskoffs_temp
                
                # Compute shift distances.
                dist = np.sqrt(np.sum(shifts[:, :2]**2, axis=1))  # pix
                dist *= self.database.obs[key]['PIXSCALE'][j]  # mas
                head, tail = os.path.split(self.database.obs[key]['FITSFILE'][j])
                log.info('  --> Recenter frames: ' + tail)
                log.info('  --> Recenter frames: median required shift = %.2f mas' % np.median(dist))
                
                # Write FITS file and PSF mask.
                head_pri['XOFFSET'] = xoffset
                head_pri['YOFFSET'] = yoffset
                head_sci['CRPIX1'] = crpix1
                head_sci['CRPIX2'] = crpix2
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                self.database.update_obs(key, j, fitsfile, maskfile, xoffset=xoffset, yoffset=yoffset, crpix1=crpix1, crpix2=crpix2)
        
        pass
    
    def find_nircam_centers(self,
                            data0,
                            key,
                            j,
                            spectral_type='G2V',
                            date=None,
                            output_dir=None,
                            oversample=2,
                            use_coeff=False):
        """
        Find the star position behind the coronagraphic mask using a WebbPSF
        model.
        
        Parameters
        ----------
        data0 : 2D-array
            Frame for which the star position shall be determined.
        key : str
            Database key of the observation containing the data0 frame.
        j : int
            Database index of the observation containing the data0 frame.
        spectral_type : str, optional
            Host star spectral type for the WebbPSF model used to determine the
            star position behind the coronagraphic mask. The default is 'G2V'.
        date : str, optional
            Observation date in the format 'YYYY-MM-DDTHH:MM:SS.MMM'. The
            default is None.
        output_dir : path, optional
            Path of the directory where the data products shall be saved. The
            default is None.
        oversample : int, optional
            Factor by which the WebbPSF model shall be oversampled. The
            default is 2.
        use_coeff : bool, optional
            Use pre-computed coefficients to generate the WebbPSF model. The
            default is False.
        
        Returns
        -------
        xc : float
            Star x-position (pix, 0-indexed).
        yc : float
            Star y-position (pix, 0-indexed).
        xshift : float
            X-shift between star and coronagraphic mask position (pix).
        yshift : float
            Y-shift between star and coronagraphic mask position (pix).
        
        """
        
        # Generate host star spectrum.
        spectrum = webbpsf_ext.stellar_spectrum(spectral_type)
        
        # Get true mask center.
        crpix1 = self.database.obs[key]['CRPIX1'][j] - 1  # 0-indexed
        crpix2 = self.database.obs[key]['CRPIX2'][j] - 1  # 0-indexed
        
        # Initialize JWST_PSF object. Use odd image size so that PSF is
        # centered in pixel center.
        log.info('  --> Recenter frames: generating WebbPSF image for absolute centering (this might take a while)')
        INSTRUME = 'NIRCam'
        FILTER = self.database.obs[key]['FILTER'][j]
        CORONMSK = self.database.obs[key]['CORONMSK'][j]
        if CORONMSK.startswith('MASKA') or CORONMSK.startswith('MASKB'):
            CORONMSK = CORONMSK[:4] + CORONMSK[5:]
        fov_pix = 65
        kwargs = {'oversample': oversample,
                  'date': date,
                  'use_coeff': use_coeff,
                  'sp': spectrum}
        psf = JWST_PSF(INSTRUME, FILTER, CORONMSK, fov_pix, **kwargs)
        
        # Get SIAF reference pixel position.
        apsiaf = psf.inst_on.siaf[self.database.obs[key]['APERNAME'][j]]
        xsciref, ysciref = (apsiaf.XSciRef, apsiaf.YSciRef)
        
        # Generate model PSF. Apply offset between SIAF reference pixel
        # position and true mask center.
        xoff = (crpix1 + 1) - xsciref
        yoff = (crpix2 + 1) - ysciref
        # crtel = apsiaf.sci_to_tel((crpix1 + 1) - xoff, (crpix2 + 1) - yoff)
        # model_psf = psf.gen_psf_idl(crtel, coord_frame='tel', return_oversample=False)
        model_psf = psf.gen_psf_idl((0, 0), coord_frame='idl', return_oversample=False)  # using this instead after discussing with Jarron
        if not np.isnan(self.database.obs[key]['BLURFWHM'][j]):
            model_psf = gaussian_filter(model_psf, self.database.obs[key]['BLURFWHM'][j])
        
        # Get transmission mask.
        yi, xi = np.indices(data0.shape)
        xtel, ytel = apsiaf.sci_to_tel(xi + 1 - xoff, yi + 1 - yoff)
        tmask, _, _ = _transmission_map(psf.inst_on, (xtel, ytel), 'tel')
        mask = tmask**2
        
        # Determine relative shift between data and model PSF. Iterate 3 times
        # to improve precision.
        xc, yc = (crpix1, crpix2)
        for i in range(3):
            
            # Crop data and transmission mask.
            datasub, xsub_indarr, ysub_indarr = ut.crop_image(image=data0,
                                                              xycen=(xc, yc),
                                                              npix=fov_pix,
                                                              return_indices=True)
            masksub = ut.crop_image(image=mask,
                                    xycen=(xc, yc),
                                    npix=fov_pix)
            
            # Determine relative shift between data and model PSF.
            yshift, xshift = phase_cross_correlation(datasub * masksub,
                                                     model_psf * masksub,
                                                     upsample_factor=1000,
                                                     normalization=None,
                                                     return_error=False)
            
            # Update star position.
            xc = np.mean(xsub_indarr) + xshift
            yc = np.mean(ysub_indarr) + yshift
        xshift, yshift = (xc - crpix1, yc - crpix2)
        log.info('  --> Recenter frames: star offset from coronagraph center (dx, dy) = (%.2f, %.2f) pix' % (xshift, yshift))
        
        # Plot data, model PSF, and scene overview.
        if output_dir is not None:
            f, ax = plt.subplots(1, 3, figsize=(3 * 6.4, 1 * 4.8))
            ax[0].imshow(datasub, origin='lower', cmap='Reds')
            ax[0].contourf(masksub, levels=[0.00, 0.25, 0.50, 0.75], cmap='Greys_r', vmin=0., vmax=2., alpha=0.5)
            ax[0].set_title('1. SCI frame & transmission mask')
            ax[1].imshow(model_psf, origin='lower', cmap='Reds')
            ax[1].contourf(masksub, levels=[0.00, 0.25, 0.50, 0.75], cmap='Greys_r', vmin=0., vmax=2., alpha=0.5)
            ax[1].set_title('Model PSF & transmission mask')
            ax[2].scatter((xsciref), (ysciref), marker='+', color='black', label='SIAF reference point')
            ax[2].scatter((crpix1 + 1), (crpix2 + 1), marker='x', color='skyblue', label='True mask center')
            ax[2].scatter((xc + 1), (yc + 1), marker='*', color='red', label='Computed star position')
            ax[2].set_aspect('equal')
            xlim = ax[2].get_xlim()
            ylim = ax[2].get_ylim()
            xrng = xlim[1]-xlim[0]
            yrng = ylim[1]-ylim[0]
            if xrng > yrng:
                ax[2].set_xlim(np.mean(xlim) - xrng, np.mean(xlim) + xrng)
                ax[2].set_ylim(np.mean(ylim) - xrng, np.mean(ylim) + xrng)
            else:
                ax[2].set_xlim(np.mean(xlim) - yrng, np.mean(xlim) + yrng)
                ax[2].set_ylim(np.mean(ylim) - yrng, np.mean(ylim) + yrng)
            ax[2].set_xlabel('x-position [pix]')
            ax[2].set_ylabel('y-position [pix]')
            ax[2].legend(loc='upper right', fontsize=12)
            ax[2].set_title('Scene overview (1-indexed)')
            plt.tight_layout()    
            plt.savefig(os.path.join(output_dir, key + '_recenter.pdf'))
            # plt.show()
            plt.close()
        
        # Return star position.
        return xc, yc, xshift, yshift
    
    def align_frames(self,
                     method='fourier',
                     align_algo='leastsq',
                     kwargs={},
                     subdir='aligned'):
        """
        Align all SCI and REF frames to the first SCI frame.
        
        Parameters
        ----------
        method : 'fourier' or 'spline' (not recommended), optional
            Method for shifting the frames. The default is 'fourier'.
        align_algo : 'leastsq' or 'header'
            Algorithm to determine the alignment offsets. Default is 'leastsq',
            'header' assumes perfect header offsets. 
        kwargs : dict, optional
            Keyword arguments for the scipy.ndimage.shift routine. The default
            is {}.
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'aligned'.
        
        Returns
        -------
        None.
        
        """
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        database_temp = deepcopy(self.database.obs)
        for i, key in enumerate(self.database.obs.keys()):
            log.info('--> Concatenation ' + key)
            
            # Find science and reference files.
            ww_sci = np.where(self.database.obs[key]['TYPE'] == 'SCI')[0]
            if len(ww_sci) == 0:
                raise UserWarning('Could not find any science files')
            ww_ref = np.where(self.database.obs[key]['TYPE'] == 'REF')[0]
            ww_all = np.append(ww_sci, ww_ref)
            
            # Loop through FITS files.
            shifts_all = []
            for j in ww_all:
                
                # Read FITS file and PSF mask.
                fitsfile = self.database.obs[key]['FITSFILE'][j]
                data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs = ut.read_obs(fitsfile)
                maskfile = self.database.obs[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                
                # Align frames.
                head, tail = os.path.split(fitsfile)
                log.info('  --> Align frames: ' + tail)
                if np.sum(np.isnan(data)) != 0:
                    raise UserWarning('Please replace nan pixels before attempting to align frames')
                shifts = []
                for k in range(data.shape[0]):
                    
                    # Take the first science frame as reference frame.
                    if j == ww_sci[0] and k == 0:
                        ref_image = data[k].copy()
                        pp = np.array([0., 0., 1.])
                        xoffset = self.database.obs[key]['XOFFSET'][j]
                        yoffset = self.database.obs[key]['YOFFSET'][j]
                        crpix1 = self.database.obs[key]['CRPIX1'][j]
                        crpix2 = self.database.obs[key]['CRPIX2'][j]
                    
                    # Align all other SCI and REF frames to the first science
                    # frame.
                    else:
                        # Calculate shifts relative to first frame. 
                        p0 = np.array([((crpix1 + xoffset) - (self.database.obs[key]['CRPIX1'][j] + self.database.obs[key]['XOFFSET'][j])) / self.database.obs[key]['PIXSCALE'][j], ((crpix2 + yoffset) - (self.database.obs[key]['CRPIX2'][j] + self.database.obs[key]['YOFFSET'][j])) / self.database.obs[key]['PIXSCALE'][j], 1.])
                        if align_algo == 'leastsq':
                            # Use header values to initiate least squares fit
                            pp = leastsq(ut.alignlsq,
                                         p0,
                                         args=(data[k], ref_image, mask, method, kwargs))[0]
                        elif align_algo == 'header':
                            # Just assume the header values are correct
                            pp = p0

                    # Append shifts to array and apply shift to image
                    # using defined method. 
                    shifts += [np.array([pp[0], pp[1], pp[2]])]
                    if j != ww_sci[0] or k != 0:
                        data[k] = ut.imshift(data[k], [shifts[k][0], shifts[k][1]], method=method, kwargs=kwargs)
                        erro[k] = ut.imshift(erro[k], [shifts[k][0], shifts[k][1]], method=method, kwargs=kwargs)
                shifts = np.array(shifts)
                if mask is not None:
                    if j != ww_sci[0]:
                        temp = np.median(shifts, axis=0)
                        # mask = ut.imshift(mask, [temp[0], temp[1]], method=method, kwargs=kwargs)
                        mask = spline_shift(mask, [temp[1], temp[0]], order=0, mode='constant', cval=np.nanmedian(mask))
                shifts_all += [shifts]
                if imshifts is not None:
                    imshifts += shifts[:, :-1]
                else:
                    imshifts = shifts[:, :-1]
                if maskoffs is not None:
                    maskoffs -= shifts[:, :-1]
                else:
                    maskoffs = -shifts[:, :-1]
                
                # Compute shift distances.
                dist = np.sqrt(np.sum(shifts[:, :2]**2, axis=1))  # pix
                dist *= self.database.obs[key]['PIXSCALE'][j]  # mas
                if j == ww_sci[0]:
                    dist = dist[1:]
                log.info('  --> Align frames: median required shift = %.2f mas' % np.median(dist))
                if self.database.obs[key]['TELESCOP'][j] == 'JWST':
                    ww = (dist < 1e-5) | (dist > 100.)
                else:
                    ww = (dist < 1e-5)
                if np.sum(ww) != 0:
                    if j == ww_sci[0]:
                        ww = np.append(np.array([False]), ww)
                    ww = np.where(ww == True)[0]
                    if align_algo != 'header':
                        log.warning('  --> The following frames might not be properly aligned: '+str(ww))
                
                # Write FITS file and PSF mask.
                head_pri['XOFFSET'] = xoffset
                head_pri['YOFFSET'] = yoffset
                head_sci['CRPIX1'] = crpix1
                head_sci['CRPIX2'] = crpix2
                fitsfile = ut.write_obs(fitsfile, output_dir, data, erro, pxdq, head_pri, head_sci, is2d, imshifts, maskoffs)
                maskfile = ut.write_msk(maskfile, mask, fitsfile)
                
                # Update spaceKLIP database.
                self.database.update_obs(key, j, fitsfile, maskfile, xoffset=xoffset, yoffset=yoffset, crpix1=crpix1, crpix2=crpix2)
            
            # Plot science frame alignment.
            colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
            f = plt.figure(figsize=(6.4, 4.8))
            ax = plt.gca()
            for index, j in enumerate(ww_sci):
                ax.scatter(shifts_all[index][:, 0] * self.database.obs[key]['PIXSCALE'][j], shifts_all[index][:, 1] * self.database.obs[key]['PIXSCALE'][j], s=5, color=colors[index], marker='o', label='PA = %.0f deg' % self.database.obs[key]['ROLL_REF'][j])
            ax.axhline(0., color='gray', lw=1)
            ax.axvline(0., color='gray', lw=1)
            ax.set_aspect('equal')
            xlim = ax.get_xlim()
            ylim = ax.get_ylim()
            xrng = xlim[1]-xlim[0]
            yrng = ylim[1]-ylim[0]
            if xrng > yrng:
                ax.set_xlim(np.mean(xlim) - xrng, np.mean(xlim) + xrng)
                ax.set_ylim(np.mean(ylim) - xrng, np.mean(ylim) + xrng)
            else:
                ax.set_xlim(np.mean(xlim) - yrng, np.mean(xlim) + yrng)
                ax.set_ylim(np.mean(ylim) - yrng, np.mean(ylim) + yrng)
            ax.set_xlabel('x-shift [mas]')
            ax.set_ylabel('y-shift [mas]')
            ax.legend(loc='upper right')
            ax.set_title('Science frame alignment')
            output_file = os.path.join(output_dir, key + '_align_sci.pdf')
            plt.savefig(output_file)
            plt.close()
            
            # Plot reference frame alignment.
            colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
            f = plt.figure(figsize=(6.4, 4.8))
            ax = plt.gca()
            seen = []
            reps = []
            syms = ['o', 'v', '^', '<', '>'] * (1 + len(ww_ref) // 5)
            add = len(ww_sci)
            for index, j in enumerate(ww_ref):
                this = '%.0f_%.0f' % (database_temp[key]['XOFFSET'][j], database_temp[key]['YOFFSET'][j])
                if this not in seen:
                    ax.scatter(shifts_all[index + add][:, 0] * self.database.obs[key]['PIXSCALE'][j], shifts_all[index + add][:, 1] * self.database.obs[key]['PIXSCALE'][j], s=5, color=colors[len(seen)], marker=syms[0], label='dpos %.0f' % (len(seen) + 1))
                    ax.hlines(-database_temp[key]['YOFFSET'][j] + yoffset, -database_temp[key]['XOFFSET'][j] + xoffset - 4., -database_temp[key]['XOFFSET'][j] + xoffset + 4., color=colors[len(seen)], lw=1)
                    ax.vlines(-database_temp[key]['XOFFSET'][j] + xoffset, -database_temp[key]['YOFFSET'][j] + yoffset - 4., -database_temp[key]['YOFFSET'][j] + yoffset + 4., color=colors[len(seen)], lw=1)
                    seen += [this]
                    reps += [1]
                else:
                    ww = np.where(np.array(seen) == this)[0][0]
                    ax.scatter(shifts_all[index + add][:, 0] * self.database.obs[key]['PIXSCALE'][j], shifts_all[index + add][:, 1] * self.database.obs[key]['PIXSCALE'][j], s=5, color=colors[ww], marker=syms[reps[ww]])
                    reps[ww] += 1
            ax.set_aspect('equal')
            xlim = ax.get_xlim()
            ylim = ax.get_ylim()
            xrng = xlim[1]-xlim[0]
            yrng = ylim[1]-ylim[0]
            if xrng > yrng:
                ax.set_xlim(np.mean(xlim) - xrng, np.mean(xlim) + xrng)
                ax.set_ylim(np.mean(ylim) - xrng, np.mean(ylim) + xrng)
            else:
                ax.set_xlim(np.mean(xlim) - yrng, np.mean(xlim) + yrng)
                ax.set_ylim(np.mean(ylim) - yrng, np.mean(ylim) + yrng)
            ax.set_xlabel('x-shift [mas]')
            ax.set_ylabel('y-shift [mas]')
            ax.legend(loc='upper right')
            ax.set_title('Reference frame alignment')
            output_file = os.path.join(output_dir, key + '_align_ref.pdf')
            plt.savefig(output_file)
            plt.close()
        
        pass