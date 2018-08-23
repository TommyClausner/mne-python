# Author(s): Tommy Clausner <tommy.clausner@gmail.com>

# License: BSD (3-clause)


import os
import warnings
import copy
import numpy as np
from scipy import sparse
from scipy.sparse import block_diag as sparse_block_diag

from .parallel import parallel_func
from .source_estimate import (VolSourceEstimate, SourceEstimate,
                              VectorSourceEstimate, _get_ico_tris)
from .source_space import SourceSpaces
from .surface import read_morph_map, mesh_edges, read_surface, _compute_nearest
from .utils import (logger, verbose, check_version, get_subjects_dir,
                    warn as warn_, deprecated)
from .externals.six import string_types
from .externals.h5io import read_hdf5, write_hdf5


def compute_source_morph(subject_from=None, subject_to='fsaverage',
                         subjects_dir=None, src=None,
                         niter_affine=(100, 100, 10), niter_sdr=(5, 5, 3),
                         spacing=5, smooth=None, warn=True, xhemi=False,
                         sparse=False, verbose=False):
    """Create a SourceMorph from one subject to another.

    Parameters
    ----------
    subject_from : str | None
        Name of the original subject as named in the SUBJECTS_DIR.
        If None src[0]['subject_his_id]' will be used (default).
    subject_to : str | array | list of two arrays
        Name of the subject to which to morph as named in the SUBJECTS_DIR.
        If morphing a surface source estimate, subject_to can also be an array
        of vertices or a list of two arrays of vertices to morph to. If
        morphing a volume source space, subject_to can be the path to a MRI
        volume. The default is 'fsaverage'.
    subjects_dir : str | None
        Path to SUBJECTS_DIR if it is not set in the environment. The default
        is None.
    src : instance of SourceSpaces | instance of SourceEstimate
        The list of SourceSpaces corresponding subject_from (can be a
        SourceEstimate if only using a surface source space).
        src must be provided when morphing a volume.
    niter_affine : tuple of int
        Number of levels (``len(niter_affine)``) and number of
        iterations per level - for each successive stage of iterative
        refinement - to perform the affine transform.
        Default is niter_affine=(100, 100, 10).
    niter_sdr : tuple of int
        Number of levels (``len(niter_sdr)``) and number of
        iterations per level - for each successive stage of iterative
        refinement - to perform the Symmetric Diffeomorphic Registration (sdr)
        transform. Default is niter_sdr=(5, 5, 3).
    spacing : tuple | int | float | list | None
        XXX THIS SHOULD BE SPLIT FOR FUTURE COMPAT WITH MIXED SOURCE SPACES!
        If morphing VolSourceEstimate, spacing is a tuple carrying the voxel
        size of volume for each spatial dimension in mm.
        If spacing is None, MRIs won't be resliced. Note that in this case
        both volumes (used to compute the morph) must have the same number of
        spatial dimensions.
        If morphing SourceEstimate or VectorSourceEstimate, spacing can be
        int, list (of two arrays), or None, defining the resolution of the
        icosahedral mesh (typically 5). If None, all vertices will be used
        (potentially filling the surface). If a list, then values will be
        morphed to the set of vertices specified in in spacing[0] and
        spacing[1].
        Note that specifying the vertices (e.g., spacing=[np.arange(10242)] * 2
        for fsaverage on a standard spacing 5 source space) can be
        substantially faster than computing vertex locations. The default is
        spacing=5.
    smooth : int | None
        Number of iterations for the smoothing of the surface data.
        If None, smooth is automatically defined to fill the surface
        with non-zero values. The default is spacing=None.
    warn : bool
        If True, warn if not all vertices were used. The default is warn=True.
    xhemi : bool
        Morph across hemisphere. Currently only implemented for
        ``subject_to == subject_from``. See notes below.
        The default is xhemi=False.
    sparse : bool
        Morph as a sparse source estimate. Works only with (Vector)
        SourceEstimate. If True the only parameters used are subject_to and
        subject_from, and spacing has to be None. Default is sparse=False.
    verbose : bool | str | int | None
        If not None, override default verbose level (see :func:`mne.verbose`
        and :ref:`Logging documentation <tut_logging>` for more). The default
        is verbose=None.

    Notes
    -----
    This function can be used to morph data between hemispheres by setting
    ``xhemi=True``. The full cross-hemisphere morph matrix maps left to right
    and right to left. A matrix for cross-mapping only one hemisphere can be
    constructed by specifying the appropriate vertices, for example, to map the
    right hemisphere to the left:
    ``vertices_from=[[], vert_rh], vertices_to=[vert_lh, []]``.

    Cross-hemisphere mapping requires appropriate ``sphere.left_right``
    morph-maps in the subject's directory. These morph maps are included
    with the ``fsaverage_sym`` FreeSurfer subject, and can be created for other
    subjects with the ``mris_left_right_register`` FreeSurfer command. The
    ``fsaverage_sym`` subject is included with FreeSurfer > 5.1 and can be
    obtained as described `here
    <http://surfer.nmr.mgh.harvard.edu/fswiki/Xhemi>`_. For statistical
    comparisons between hemispheres, use of the symmetric ``fsaverage_sym``
    model is recommended to minimize bias [1]_.

    .. versionadded:: 0.17.0

    References
    ----------
    .. [1] Greve D. N., Van der Haegen L., Cai Q., Stufflebeam S., Sabuncu M.
           R., Fischl B., Brysbaert M.
           A Surface-based Analysis of Language Lateralization and Cortical
           Asymmetry. Journal of Cognitive Neuroscience 25(9), 1477-1492, 2013.
    """
    if isinstance(src, (SourceEstimate, VectorSourceEstimate)):
        src_data = copy.deepcopy(src.vertices)
        kind = 'surface'
        subject_from = _check_subject_from(subject_from, src.subject)
    elif src is None:
        raise ValueError('src must be supplied, got None')
    else:
        src_data, kind = _get_src_data(src)
        subject_from = _check_subject_from(subject_from, src)
    del src
    # Params
    warn = False if sparse else warn

    if kind not in 'surface' and xhemi:
        raise ValueError('Inter-hemispheric morphing can only be used '
                         'with surface source estimates.')
    if sparse and kind != 'surface':
        raise ValueError('Only surface source estimates can compute a '
                         'sparse morph.')

    subjects_dir = get_subjects_dir(subjects_dir, raise_error=True)

    # VolSourceEstimate
    morph_shape = morph_zooms = morph_affine = None
    pre_sdr_affine = sdr_mapping = None
    morph_mat = vertices_to = None
    if kind == 'volume':
        assert subject_to is not None  # guaranteed by _check_subject_from

        _check_dep(nibabel='2.1.0', dipy=False)

        logger.info('volume source space inferred...')
        import nibabel as nib

        # load moving MRI
        mri_subpath = os.path.join('mri', 'brain.mgz')
        mri_path_from = os.path.join(subjects_dir, subject_from,
                                     mri_subpath)

        logger.info('loading %s as moving volume' % mri_path_from)
        with warnings.catch_warnings():
            mri_from = nib.load(mri_path_from)

        # load static MRI
        static_path = os.path.join(subjects_dir, subject_to)

        if not os.path.isdir(static_path):
            mri_path_to = static_path
        else:
            mri_path_to = os.path.join(static_path, mri_subpath)

        if os.path.isfile(mri_path_to):
            logger.info('loading %s as static volume' % mri_path_to)
            with warnings.catch_warnings():
                mri_to = nib.load(mri_path_to)
        else:
            raise IOError('cannot read file: %s' % mri_path_to)

        # pre-compute non-linear morph
        morph_shape, morph_zooms, morph_affine, pre_sdr_affine, sdr_mapping = \
            _compute_morph_sdr(
                mri_from, mri_to, niter_affine, niter_sdr, spacing)
    elif kind == 'surface':
        logger.info('surface source space inferred...')
        if sparse:
            if spacing is not None:
                raise RuntimeError('spacing must be set to None if '
                                   'sparse=True.')
            vertices_to, morph_mat = _compute_sparse_morph(
                src_data, subject_from, subject_to, subjects_dir)
        else:
            vertices_to = grade_to_vertices(
                subject_to, spacing, subjects_dir, 1)
            morph_mat = _compute_morph_matrix(
                subject_from=subject_from, subject_to=subject_to,
                vertices_from=src_data, vertices_to=vertices_to,
                subjects_dir=subjects_dir, smooth=smooth, warn=warn,
                xhemi=xhemi)
            n_verts = sum(len(v) for v in vertices_to)
            assert morph_mat.shape[0] == n_verts

    return SourceMorph(subject_from, subject_to, kind,
                       niter_affine, niter_sdr, spacing, smooth, xhemi,
                       morph_mat, vertices_to, morph_shape, morph_zooms,
                       morph_affine, pre_sdr_affine, sdr_mapping, src_data)


def _compute_sparse_morph(vertices_from, subject_from, subject_to,
                          subjects_dir=None):
    """Get nearest vertices from one subject to another."""
    maps = read_morph_map(subject_to, subject_from, subjects_dir)
    cnt = 0
    vertices = list()
    cols = list()
    for verts, map_hemi in zip(vertices_from, maps):
        vertno_h = _sparse_argmax_nnz_row(map_hemi[verts])
        order = np.argsort(vertno_h)
        cols.append(cnt + order)
        vertices.append(vertno_h[order])
        cnt += len(vertno_h)
    cols = np.concatenate(cols)
    rows = np.arange(len(cols))
    data = np.ones(len(cols))
    morph_mat = sparse.coo_matrix((data, (rows, cols)),
                                  shape=(len(cols), len(cols))).tocsr()
    return vertices, morph_mat


_SOURCE_MORPH_ATTRIBUTES = [  # used in writing
    'subject_from', 'subject_to', 'kind', 'niter_affine', 'niter_sdr',
    'spacing', 'smooth', 'xhemi', 'morph_mat', 'vertices_to',
    'morph_shape', 'morph_zooms', 'morph_affine', 'pre_sdr_affine',
    'sdr_mapping', 'src_data']


class SourceMorph(object):
    """Morph source space data from one subject to another.

    Attributes
    ----------
    subject_from : str | None
        Name of the subject from which to morph as named in the SUBJECTS_DIR
    subject_to : str | array | list of two arrays
        Name of the subject on which to morph as named in the SUBJECTS_DIR.
        The default is 'fsaverage'. If morphing a surface source extimate,
        subject_to can also be an array of vertices or a list of two arrays of
        vertices to morph to. If morphing a volume source space, subject_to can
        be the path to a MRI volume.
    kind : str | None
        Kind of source estimate. E.g. 'volume' or 'surface'.
    niter_affine : tuple of int
        Number of levels (``len(niter_affine)``) and number of
        iterations per level - for each successive stage of iterative
        refinement - to perform the affine transform.
        Default is niter_affine=(100, 100, 10)
    niter_sdr : tuple of int
        Number of levels (``len(niter_sdr)``) and number of
        iterations per level - for each successive stage of iterative
        refinement - to perform the Symmetric Diffeomorphic Registration (sdr)
        transform. Default is niter_sdr=(5, 5, 3)
    spacing : tuple | int | float | list | None
        If morphing VolSourceEstimate, spacing is a tuple, carrying the
        voxel size of the MRI volume for each spatial dimension in mm or int
        for isotropic voxel size in mm.
        If spacing is None, MRIs won't be resliced. Note that in this case
        both volumes (used to compute the morph) must have the same number of
        slices for each spatial dimensions.
        If morphing SourceEstimate or VectorSourceEstimate, spacing can be
        int, list (of two arrays), or None, defining the resolution of the
        icosahedral mesh (typically 5). If None, all vertices will be used
        (potentially filling the surface). If a list, then values will be
        morphed to the set of vertices specified in in spacing[0] and
        spacing[1].
        Note that specifying the vertices (e.g., spacing=[np.arange(10242),
        np.arange(10242)] for fsaverage on a standard spacing 5 source space)
        can be substantially faster than computing vertex locations.
    smooth : int | None
        Number of iterations for the smoothing of the surface data.
        If None, smooth is automatically defined to fill the surface
        with non-zero values.
    xhemi : bool
        Morph across hemisphere.
    """

    def __init__(self, subject_from, subject_to, kind,
                 niter_affine, niter_sdr, spacing, smooth, xhemi,
                 morph_mat, vertices_to, morph_shape, morph_zooms,
                 morph_affine, pre_sdr_affine, sdr_mapping, src_data):
        # universal
        self.subject_from = subject_from
        self.subject_to = subject_to
        self.kind = kind
        # vol input
        self.niter_affine = niter_affine
        self.niter_sdr = niter_sdr
        self.spacing = spacing
        # surf input
        self.smooth = smooth
        self.xhemi = xhemi
        # surf computed
        self.morph_mat = morph_mat
        self.vertices_to = vertices_to
        # vol computed
        self.morph_shape = morph_shape
        self.morph_zooms = morph_zooms
        self.morph_affine = morph_affine
        self.pre_sdr_affine = pre_sdr_affine
        self.sdr_mapping = sdr_mapping
        self.src_data = src_data

    @verbose
    def __call__(self, stc_from, as_volume=False, mri_resolution=False,
                 mri_space=False, apply_morph=True, format='nifti1',
                 verbose=None):
        """Morph source space data.

        Parameters
        ----------
        stc_from : VolSourceEstimate | SourceEstimate | VectorSourceEstimate
            The source estimate to morph.
        as_volume : bool
            Whether to output a NIfTI volume. stc_from has to be a
            VolSourceEstimate. The default is as_volume=False.
        mri_resolution: bool | tuple | int | float
            If True the image is saved in MRI resolution. Default False.
            WARNING: if you have many time points the file produced can be
            huge. The default is mri_resolution=False.
        mri_space : bool
            Whether the image to world registration should be in mri space. The
            default is mri_space=mri_resolution.
        apply_morph : bool
            If as_volume=True and apply_morph=True, the input stc will be
            morphed and outputted as a volume. The default is as_volume=True.
        format : str
            Either 'nifti1' (default) or 'nifti2'.
        verbose : bool | str | int | None
            If not None, override default verbose level (see
            :func:`mne.verbose` and :ref:`Logging documentation <tut_logging>`
            for more). The default is verbose=None.

        Returns
        -------
        stc_to : VolSourceEstimate | SourceEstimate | VectorSourceEstimate | Nifti1Image | Nifti2Image
            The morphed source estimate or a NIfTI image if as_volume=True.
            By default a Nifti1Image is returned. See 'format'.
        """  # noqa: E501
        stc = copy.deepcopy(stc_from)

        if as_volume:
            # if no mri resolution is desired, probably no mri space is wanted
            # as well and vice versa
            mri_space = mri_resolution if mri_space is None else mri_space
            return self.as_volume(
                stc, fname=None, mri_resolution=mri_resolution,
                mri_space=mri_space, apply_morph=apply_morph, format=format)

        if stc.subject is None:
            stc.subject = self.subject_from

        if self.subject_from is None:
            self.subject_from = stc.subject

        if stc.subject != self.subject_from:
            raise ValueError('stc_from.subject and '
                             'morph.subject_from must match. (%s != %s)' %
                             (stc.subject, self.subject_from))

        return _apply_morph_data(self, stc)

    def __repr__(self):  # noqa: D105
        s = "%s" % self.kind
        s += ", subject_from : %s" % self.subject_from
        s += ", subject_to : %s" % self.subject_to
        s += ", spacing : {}".format(self.spacing)
        if self.kind == 'volume':
            s += ", niter_affine : {}".format(self.niter_affine)
            s += ", niter_sdr : {}".format(self.niter_sdr)

        elif self.kind == 'surface' or self.kind == 'vector':
            s += ", smooth : %s" % self.smooth
            s += ", xhemi : %s" % self.xhemi

        return "<SourceMorph  |  %s>" % s

    @verbose
    def save(self, fname, overwrite=False, verbose=None):
        """Save the morph for source estimates to a file.

        Parameters
        ----------
        fname : str
            The stem of the file name. '-morph.h5' will be added if fname does
            not end with '.h5'
        overwrite : bool
            If True, overwrite existing file.
        verbose : bool | str | int | None
            If not None, override default verbose level (see
            :func:`mne.verbose` and :ref:`Logging documentation <tut_logging>`
            for more).
        """
        if not fname.endswith('.h5'):
            fname = '%s-morph.h5' % fname

        out_dict = dict((key, getattr(self, key))
                        for key in _SOURCE_MORPH_ATTRIBUTES)
        for key in ('pre_sdr_affine', 'sdr_mapping'):  # classes
            if out_dict[key] is not None:
                out_dict[key] = out_dict[key].__dict__
        write_hdf5(fname, out_dict, overwrite=overwrite)

    def as_volume(self, stc, fname=None, mri_resolution=False, mri_space=True,
                  apply_morph=False, format='nifti1'):
        """Return volume source space as Nifti1Image and / or save to disk.

        Parameters
        ----------
        stc : VolSourceEstimate
            Data to be transformed
        fname : str | None
            String to where to save the volume. If not None that volume will
            be saved at fname.
        mri_resolution: bool | tuple | int | float
            Whether to use MRI resolution. If False the morph's resolution
            will be used. If tuple the voxel size must be given in float values
            in mm. E.g. mri_resolution=(3., 3., 3.). The default is
            mri_resolution=False.
            WARNING: if you have many time points the file produced can be
            huge.
        mri_space : bool
            Whether the image to world registration should be in MRI space. The
            default is mri_space=True.
        apply_morph : bool
            Whether to apply the precomputed morph to stc or not. The default
            is apply_morph=False.
        format : str
            Either 'nifti1' (default) or 'nifti2'

        Returns
        -------
        img : Nifti1Image | Nifti2Image
            The image object.
        """
        if format != 'nifti1' and format != 'nifti2':
            raise ValueError("invalid format %s, must be 'nifti1' or 'nifti1''"
                             % format)
        if apply_morph:
            stc = self.__call__(stc)  # apply morph if desired
        img = _stc_as_volume(self, stc, mri_resolution=mri_resolution,
                             mri_space=mri_space, format=format)
        if fname is not None:
            import nibabel as nib
            nib.save(img, fname)
        return img


###############################################################################
# I/O
def _check_subject_from(subject_from, src):
    if isinstance(src, string_types):
        subject_check = src
    else:
        subject_check = src[0]['subject_his_id']
    if subject_from is None:
        subject_from = subject_check
    elif subject_check is not None and subject_from != subject_check:
        raise ValueError('subject_from does not match source space subject'
                         ' (%s != %s)' % (subject_from, subject_check))
    if subject_from is None:
        raise ValueError('subject_from could not be inferred, it must be '
                         'specified')
    return subject_from


def read_source_morph(fname):
    """Load the morph for source estimates from a file.

    Parameters
    ----------
    fname : str
        Full filename including path.

    Returns
    -------
    source_morph : instance of SourceMorph
        The loaded morph.
    """
    vals = read_hdf5(fname)
    if vals['pre_sdr_affine'] is not None:  # reconstruct
        from dipy.align.imaffine import AffineMap
        affine = vals['pre_sdr_affine']
        vals['pre_sdr_affine'] = AffineMap(None)
        vals['pre_sdr_affine'].__dict__ = affine
    if vals['sdr_mapping'] is not None:
        from dipy.align.imwarp import DiffeomorphicMap
        morph = vals['sdr_mapping']
        vals['sdr_mapping'] = DiffeomorphicMap(None, [])
        vals['sdr_mapping'].__dict__ = morph
    return SourceMorph(**vals)


###############################################################################
# Helper functions for SourceMorph methods
def _check_dep(nibabel='2.1.0', dipy='0.10.1'):
    """Check dependencies."""
    for lib, ver in zip(['nibabel', 'dipy'],
                        [nibabel, dipy]):
        passed = True if not ver else check_version(lib, ver)

        if not passed:
            raise ImportError('%s %s or higher must be correctly '
                              'installed and accessible from Python' % (lib,
                                                                        ver))


def _stc_as_volume(morph, stc, mri_resolution=False, mri_space=True,
                   format='nifti1'):
    """Return volume source space as Nifti1Image and/or save to disk."""
    if not isinstance(stc, VolSourceEstimate):
        raise ValueError('Only volume source estimates can be converted to '
                         'volumes')
    _check_dep(nibabel='2.1.0', dipy=False)

    if format == 'nifti1':
        from nibabel import (Nifti1Image as NiftiImage,
                             Nifti1Header as NiftiHeader)
    elif format == 'nifti2':
        from nibabel import (Nifti2Image as NiftiImage,
                             Nifti2Header as NiftiHeader)

    from dipy.align.reslice import reslice

    new_zooms = None

    # if full MRI resolution, compute zooms from shape and MRI zooms
    if isinstance(mri_resolution, bool) and mri_resolution:
        new_zooms = _get_zooms_orig(morph)

    # if MRI resolution is set manually as a single value, convert to tuple
    if isinstance(mri_resolution, (int, float)) and not isinstance(
            mri_resolution, bool):
        # use iso voxel size
        new_zooms = (float(mri_resolution),) * 3

    # if MRI resolution is set manually as a tuple, use it
    if isinstance(mri_resolution, tuple):
        new_zooms = mri_resolution

    # setup volume properties
    shape = tuple([int(i) for i in morph.morph_shape])
    affine = morph.morph_affine
    zooms = morph.morph_zooms[:3]

    # create header
    hdr = NiftiHeader()
    hdr.set_xyzt_units('mm', 'msec')
    hdr['pixdim'][4] = 1e3 * stc.tstep

    # setup empty volume
    img = np.zeros(shape + (stc.shape[1],)).reshape(-1, stc.shape[1])
    img[stc.vertices, :] = stc.data

    img = img.reshape(shape + (-1,))

    # make nifti from data
    with warnings.catch_warnings():  # nibabel<->numpy warning
        img = NiftiImage(img, affine, header=hdr)

    # reslice in case of manually defined voxel size
    if new_zooms is not None:
        new_zooms = new_zooms[:3]
        img, affine = reslice(img.get_data(),
                              img.affine,  # MRI to world registration
                              zooms,  # old voxel size in mm
                              new_zooms)  # new voxel size in mm
        with warnings.catch_warnings():  # nibabel<->numpy warning
            img = NiftiImage(img, affine)
        zooms = new_zooms

    #  set zooms in header
    img.header.set_zooms(tuple(zooms) + (1,))
    return img


def _get_src_data(src):
    """Obtain src data relevant for as _volume."""
    src_data = dict()

    # copy data to avoid conflicts
    if isinstance(src, SourceEstimate):
        src_t = [dict(vertno=src.vertices[0]), dict(vertno=src.vertices[1])]
        src_kind = 'surface'
    elif isinstance(src, SourceSpaces):
        src_t = src.copy()
        src_kind = src.kind
    else:
        raise TypeError('src must be an instance of SourceSpaces or '
                        'SourceEstimate, got %s (%s)' % (type(src), src))
    del src

    # extract all relevant data for volume operations
    if src_kind == 'volume':
        shape = src_t[0]['shape']
        src_data.update({'src_shape': (shape[2], shape[1], shape[0]),
                         'src_affine_vox': src_t[0]['vox_mri_t']['trans'],
                         'src_affine_src': src_t[0]['src_mri_t']['trans'],
                         'src_affine_ras': src_t[0]['mri_ras_t']['trans'],
                         'src_shape_full': (
                             src_t[0]['mri_height'], src_t[0]['mri_depth'],
                             src_t[0]['mri_width']),
                         'interpolator': src_t[0]['interpolator'],
                         'inuse': src_t[0]['inuse']})
    else:
        assert src_kind == 'surface'
        src_data = [s['vertno'].copy() for s in src_t]

    # delete copy
    return src_data, src_kind


def _interpolate_data(stc, morph, mri_resolution=True, mri_space=True,
                      format='nifti1'):
    """Interpolate source estimate data to MRI."""
    _check_dep(nibabel='2.1.0', dipy=False)
    if format != 'nifti1' and format != 'nifti2':
        raise ValueError("invalid format specifier %s. Must be 'nifti1' or"
                         " 'nifti2'" % format)
    if format == 'nifti1':
        from nibabel import (Nifti1Image as NiftiImage,
                             Nifti1Header as NiftiHeader)
    elif format == 'nifti2':
        from nibabel import (Nifti2Image as NiftiImage,
                             Nifti2Header as NiftiHeader)
    assert morph.kind == 'volume'

    voxel_size_defined = False

    if isinstance(mri_resolution, (int, float)) and not isinstance(
            mri_resolution, bool):
        # use iso voxel size
        mri_resolution = (float(mri_resolution),) * 3

    if isinstance(mri_resolution, tuple):
        _check_dep(nibabel=False, dipy='0.10.1')  # nibabel was already checked
        from dipy.align.reslice import reslice

        voxel_size = mri_resolution
        voxel_size_defined = True
        mri_resolution = True

    # if data wasn't morphed yet - necessary for call of
    # stc_unmorphed.as_volume. Since only the shape of src is known, it cannot
    # be resliced to a given voxel size without knowing the original.
    if isinstance(morph, SourceSpaces):
        assert morph.kind == 'volume'
        if voxel_size_defined:
            raise ValueError(
                "Cannot infer original voxel size for reslicing... "
                "set mri_resolution to boolean value or apply morph first.")
        from mne.io.constants import BunchConst
        morph = BunchConst(src_data=_get_src_data(morph)[0])

    # setup volume parameters
    n_times = stc.data.shape[1]
    shape3d = morph.src_data['src_shape']
    shape = (n_times,) + shape3d
    vols = np.zeros(shape)

    mask3d = morph.src_data['inuse'].reshape(shape3d).astype(np.bool)
    n_vertices = np.sum(mask3d)

    n_vertices_seen = 0
    for k, vol in enumerate(vols):  # loop over time instants
        stc_slice = slice(n_vertices_seen, n_vertices_seen + n_vertices)
        vol[mask3d] = stc.data[stc_slice, k]

    n_vertices_seen += n_vertices

    # use mri resolution as represented in src
    if mri_resolution:
        mri_shape3d = morph.src_data['src_shape_full']
        mri_shape = (n_times,) + mri_shape3d
        mri_vol = np.zeros(mri_shape)

        interpolator = morph.src_data['interpolator']

        for k, vol in enumerate(vols):
            mri_vol[k] = (interpolator * vol.ravel()).reshape(mri_shape3d)
        vols = mri_vol

    vols = vols.T

    # set correct space
    affine = morph.src_data['src_affine_vox']

    if not mri_resolution:
        affine = morph.src_data['src_affine_src']

    if mri_space:
        affine = np.dot(morph.src_data['src_affine_ras'], affine)

    affine[:3] *= 1e3

    # pre-define header
    header = NiftiHeader()
    header.set_xyzt_units('mm', 'msec')
    header['pixdim'][4] = 1e3 * stc.tstep

    with warnings.catch_warnings():  # nibabel<->numpy warning
        img = NiftiImage(vols, affine, header=header)

    # if a specific voxel size was targeted (only possible after morphing)
    if voxel_size_defined:
        # reslice mri
        img, img_affine = reslice(
            img.get_data(), img.affine, _get_zooms_orig(morph), voxel_size)
        with warnings.catch_warnings():  # nibabel<->numpy warning
            img = NiftiImage(img, img_affine, header=header)

    return img


###############################################################################
# Morph for VolSourceEstimate

def _compute_morph_sdr(mri_from, mri_to, niter_affine=(100, 100, 10),
                       niter_sdr=(5, 5, 3), morph_zooms=(5., 5., 5.)):
    """Get a matrix that morphs data from one subject to another."""
    _check_dep(nibabel='2.1.0', dipy='0.10.1')
    import nibabel as nib
    from pytest import warns as warns_numpy
    with warns_numpy(None):  # dipy <-> numpy warning
        from dipy.align import imaffine, imwarp, metrics, transforms
    from dipy.align.reslice import reslice

    logger.info('Computing nonlinear Symmetric Diffeomorphic Registration...')

    # use voxel size of mri_from
    if morph_zooms is None:
        morph_zooms = mri_from.header.get_zooms()[:3]

    # use iso voxel size
    if isinstance(morph_zooms, (int, float)):
        morph_zooms = (float(morph_zooms),) * 3

    # reslice mri_from
    mri_from_res, mri_from_res_affine = reslice(
        mri_from.get_data(), mri_from.affine, mri_from.header.get_zooms()[:3],
        morph_zooms)

    with warnings.catch_warnings():  # nibabel<->numpy warning
        mri_from = nib.Nifti1Image(mri_from_res, mri_from_res_affine)

    # reslice mri_to
    mri_to_res, mri_to_res_affine = reslice(
        mri_to.get_data(), mri_to.affine, mri_to.header.get_zooms()[:3],
        morph_zooms)

    with warnings.catch_warnings():  # nibabel<->numpy warning
        mri_to = nib.Nifti1Image(mri_to_res, mri_to_res_affine)

    mri_to_grid2world = mri_to.affine
    mri_to = np.array(mri_to.dataobj, float)  # to ndarray
    mri_to /= mri_to.max()
    mri_from_grid2world = mri_from.affine  # get mri_from to world transform
    mri_from = np.array(mri_from.dataobj, float)  # to ndarray
    mri_from /= mri_from.max()  # normalize

    # compute center of mass
    c_of_mass = imaffine.transform_centers_of_mass(
        mri_to, mri_to_grid2world, mri_from, mri_from_grid2world)

    # set up Affine Registration
    affreg = imaffine.AffineRegistration(
        metric=imaffine.MutualInformationMetric(nbins=32),
        level_iters=list(niter_affine),
        sigmas=[3.0, 1.0, 0.0],
        factors=[4, 2, 1])

    # translation
    translation = affreg.optimize(mri_to, mri_from,
                                  transforms.TranslationTransform3D(), None,
                                  mri_to_grid2world, mri_from_grid2world,
                                  starting_affine=c_of_mass.affine)

    # rigid body transform (translation + rotation)
    rigid = affreg.optimize(mri_to, mri_from,
                            transforms.RigidTransform3D(), None,
                            mri_to_grid2world, mri_from_grid2world,
                            starting_affine=translation.affine)

    # affine transform (translation + rotation + scaling)
    affine = affreg.optimize(mri_to, mri_from,
                             transforms.AffineTransform3D(), None,
                             mri_to_grid2world, mri_from_grid2world,
                             starting_affine=rigid.affine)

    # apply affine transformation
    mri_from_affine = affine.transform(mri_from)

    # set up Symmetric Diffeomorphic Registration (metric, iterations)
    sdr = imwarp.SymmetricDiffeomorphicRegistration(
        metrics.CCMetric(3), list(niter_sdr))

    # compute mapping
    mapping = sdr.optimize(mri_to, mri_from_affine)
    morph_shape = tuple(mapping.domain_shape.astype('float'))
    logger.info('done.')
    return morph_shape, morph_zooms, mri_to_grid2world, affine, mapping


###############################################################################
# Morph for SourceEstimate |  VectorSourceEstimate
@deprecated("This function is deprecated and will be removed in version 0.19. "
            "Use morph_mat = mne.compute_source_morph(...)morph_mat")
def compute_morph_matrix(subject_from, subject_to, vertices_from, vertices_to,
                         smooth=None, subjects_dir=None, warn=True,
                         xhemi=False, verbose=None):
    """Get a matrix that morphs data from one subject to another.

    Parameters
    ----------
    subject_from : str
        Name of the original subject as named in the SUBJECTS_DIR.
    subject_to : str
        Name of the subject on which to morph as named in the SUBJECTS_DIR.
    vertices_from : list of arrays of int
        Vertices for each hemisphere (LH, RH) for subject_from.
    vertices_to : list of arrays of int
        Vertices for each hemisphere (LH, RH) for subject_to.
    smooth : int or None
        Number of iterations for the smoothing of the surface data.
        If None, smooth is automatically defined to fill the surface
        with non-zero values. The default is smooth=None.
    subjects_dir : str
        Path to SUBJECTS_DIR is not set in the environment. The default is
        subjects_dir=None.
    warn : bool
        If True, warn if not all vertices were used. warn
    xhemi : bool
        Morph across hemisphere. Currently only implemented for
        ``subject_to == subject_from``. See notes below. The default is
        xhemi=False.
    verbose : bool, str, int, or None
        If not None, override default verbose level (see :func:`mne.verbose`
        and :ref:`Logging documentation <tut_logging>` for more). The default
        is verbose=None.

    Returns
    -------
    morph_matrix : sparse matrix
        matrix that morphs data from ``subject_from`` to ``subject_to``.

    Notes
    -----
    This function can be used to morph data between hemispheres by setting
    ``xhemi=True``. The full cross-hemisphere morph matrix maps left to right
    and right to left. A matrix for cross-mapping only one hemisphere can be
    constructed by specifying the appropriate vertices, for example, to map the
    right hemisphere to the left:
    ``vertices_from=[[], vert_rh], vertices_to=[vert_lh, []]``.

    Cross-hemisphere mapping requires appropriate ``sphere.left_right``
    morph-maps in the subject's directory. These morph maps are included
    with the ``fsaverage_sym`` FreeSurfer subject, and can be created for other
    subjects with the ``mris_left_right_register`` FreeSurfer command. The
    ``fsaverage_sym`` subject is included with FreeSurfer > 5.1 and can be
    obtained as described `here
    <http://surfer.nmr.mgh.harvard.edu/fswiki/Xhemi>`_. For statistical
    comparisons between hemispheres, use of the symmetric ``fsaverage_sym``
    model is recommended to minimize bias [1]_.

    References
    ----------
    .. [1] Greve D. N., Van der Haegen L., Cai Q., Stufflebeam S., Sabuncu M.
           R., Fischl B., Brysbaert M.
           A Surface-based Analysis of Language Lateralization and Cortical
           Asymmetry. Journal of Cognitive Neuroscience 25(9), 1477-1492, 2013.
    """
    return _compute_morph_matrix(subject_from, subject_to, vertices_from,
                                 vertices_to, smooth, subjects_dir, warn,
                                 xhemi)


def _compute_morph_matrix(subject_from, subject_to, vertices_from, vertices_to,
                          smooth=None, subjects_dir=None, warn=True,
                          xhemi=False):
    """Compute morph matrix."""
    logger.info('Computing morph matrix...')
    subjects_dir = get_subjects_dir(subjects_dir, raise_error=True)

    tris = _get_subject_sphere_tris(subject_from, subjects_dir)
    maps = read_morph_map(subject_from, subject_to, subjects_dir, xhemi)

    # morph the data

    if xhemi:
        hemi_indexes = [(0, 1), (1, 0)]
        vertices_to.reverse()
    else:
        hemi_indexes = [(0, 0), (1, 1)]
    morpher = []
    for hemi_from, hemi_to in hemi_indexes:
        idx_use = vertices_from[hemi_from]
        if len(idx_use) == 0:
            continue
        e = mesh_edges(tris[hemi_from])
        e.data[e.data == 2] = 1
        n_vertices = e.shape[0]
        e = e + sparse.eye(n_vertices, n_vertices)
        m = sparse.eye(len(idx_use), len(idx_use), format='csr')
        mm = _morph_buffer(m, idx_use, e, smooth, n_vertices,
                           vertices_to[hemi_to], maps[hemi_from], warn=warn)
        morpher.append(mm)

    if len(morpher) == 0:
        raise ValueError("Empty morph-matrix")
    elif len(morpher) == 1:
        morpher = morpher[0]
    else:
        morpher = sparse_block_diag(morpher, format='csr')
    logger.info('[done]')
    return morpher


@verbose
def grade_to_vertices(subject, grade, subjects_dir=None, n_jobs=1,
                      verbose=None):
    """Convert a grade to source space vertices for a given subject.

    Parameters
    ----------
    subject : str
        Name of the subject
    grade : int | list
        Resolution of the icosahedral mesh (typically 5). If None, all
        vertices will be used (potentially filling the surface). If a list,
        then values will be morphed to the set of vertices specified in
        in grade[0] and grade[1]. Note that specifying the vertices (e.g.,
        grade=[np.arange(10242), np.arange(10242)] for fsaverage on a
        standard grade 5 source space) can be substantially faster than
        computing vertex locations. Note that if subject='fsaverage'
        and 'grade=5', this set of vertices will automatically be used
        (instead of computed) for speed, since this is a common morph.
    subjects_dir : str | None
        Path to SUBJECTS_DIR if it is not set in the environment
    n_jobs : int
        Number of jobs to run in parallel. The default is n_jobs=1.
    verbose : bool, str, int, or None
        If not None, override default verbose level (see :func:`mne.verbose`
        and :ref:`Logging documentation <tut_logging>` for more).

    Returns
    -------
    vertices : list of arrays of int
        Vertex numbers for LH and RH
    """
    # add special case for fsaverage for speed
    if subject == 'fsaverage' and isinstance(grade, int) and grade == 5:
        return [np.arange(10242)] * 2
    subjects_dir = get_subjects_dir(subjects_dir, raise_error=True)

    spheres_to = [os.path.join(subjects_dir, subject, 'surf',
                               xh + '.sphere.reg') for xh in ['lh', 'rh']]
    lhs, rhs = [read_surface(s)[0] for s in spheres_to]

    if grade is not None:  # fill a subset of vertices
        if isinstance(grade, list):
            if not len(grade) == 2:
                raise ValueError('grade as a list must have two elements '
                                 '(arrays of output vertices)')
            vertices = grade
        else:
            # find which vertices to use in "to mesh"
            ico = _get_ico_tris(grade, return_surf=True)
            lhs /= np.sqrt(np.sum(lhs ** 2, axis=1))[:, None]
            rhs /= np.sqrt(np.sum(rhs ** 2, axis=1))[:, None]

            # Compute nearest vertices in high dim mesh
            parallel, my_compute_nearest, _ = \
                parallel_func(_compute_nearest, n_jobs)
            lhs, rhs, rr = [a.astype(np.float32)
                            for a in [lhs, rhs, ico['rr']]]
            vertices = parallel(my_compute_nearest(xhs, rr)
                                for xhs in [lhs, rhs])
            # Make sure the vertices are ordered
            vertices = [np.sort(verts) for verts in vertices]
            for verts in vertices:
                if (np.diff(verts) == 0).any():
                    raise ValueError(
                        'Cannot use icosahedral grade %s with subject %s, '
                        'mapping %s vertices onto the high-resolution mesh '
                        'yields repeated vertices, use a lower grade or a '
                        'list of vertices from an existing source space'
                        % (grade, subject, len(verts)))
    else:  # potentially fill the surface
        vertices = [np.arange(lhs.shape[0]), np.arange(rhs.shape[0])]

    return vertices


def _morph_buffer(data, idx_use, e, smooth, n_vertices, nearest, maps,
                  warn=True):
    """Morph data from one subject's source space to another.

    Parameters
    ----------
    data : array, or csr sparse matrix
        A n_vertices [x 3] x n_times (or other dimension) dataset to morph.
    idx_use : array of int
        Vertices from the original subject's data.
    e : sparse matrix
        The mesh edges of the "from" subject.
    smooth : int
        Number of smoothing iterations to perform. A hard limit of 100 is
        also imposed.
    n_vertices : int
        Number of vertices.
    nearest : array of int
        Vertices on the reference surface to use.
    maps : sparse matrix
        Morph map from one subject to the other.
    warn : bool
        If True, warn if not all vertices were used.
    verbose : bool, str, int, or None
        If not None, override default verbose level (see :func:`mne.verbose`
        and :ref:`Logging documentation <tut_logging>` for more). The default
        is verbose=None.

    Returns
    -------
    data_morphed : array, or csr sparse matrix
        The morphed data (same type as input).
    """
    # When operating on vector data, morph each dimension separately
    if data.ndim == 3:
        data_morphed = np.zeros((len(nearest), 3, data.shape[2]),
                                dtype=data.dtype)
        for dim in range(3):
            data_morphed[:, dim, :] = _morph_buffer(
                data=data[:, dim, :], idx_use=idx_use, e=e, smooth=smooth,
                n_vertices=n_vertices, nearest=nearest, maps=maps, warn=warn)
        return data_morphed

    n_iter = 99  # max nb of smoothing iterations (minus one)
    if smooth is not None:
        if smooth <= 0:
            raise ValueError('The number of smoothing operations ("smooth") '
                             'has to be at least 1.')
        smooth -= 1
    # make sure we're in CSR format
    e = e.tocsr()
    if sparse.issparse(data):
        use_sparse = True
        if not isinstance(data, sparse.csr_matrix):
            data = data.tocsr()
    else:
        use_sparse = False

    done = False
    # do the smoothing
    for k in range(n_iter + 1):
        # get the row sum
        mult = np.zeros(e.shape[1])
        mult[idx_use] = 1
        idx_use_data = idx_use
        data_sum = e * mult

        # new indices are non-zero sums
        idx_use = np.where(data_sum)[0]

        # typically want to make the next iteration have these indices
        idx_out = idx_use

        # figure out if this is the last iteration
        if smooth is None:
            if k == n_iter or len(idx_use) >= n_vertices:
                # stop when vertices filled
                idx_out = None
                done = True
        elif k == smooth:
            idx_out = None
            done = True

        # do standard smoothing multiplication
        data = _morph_mult(data, e, use_sparse, idx_use_data, idx_out)

        if done is True:
            break

        # do standard normalization
        if use_sparse:
            data.data /= data_sum[idx_use].repeat(np.diff(data.indptr))
        else:
            data /= data_sum[idx_use][:, None]

    # do special normalization for last iteration
    if use_sparse:
        data_sum[data_sum == 0] = 1
        data.data /= data_sum.repeat(np.diff(data.indptr))
    else:
        data[idx_use, :] /= data_sum[idx_use][:, None]
    if len(idx_use) != len(data_sum) and warn:
        warn_('%s/%s vertices not included in smoothing, consider increasing '
              'the number of steps'
              % (len(data_sum) - len(idx_use), len(data_sum)))

    logger.info('    %d smooth iterations done.' % (k + 1))

    data_morphed = maps[nearest, :] * data
    return data_morphed


def _morph_mult(data, e, use_sparse, idx_use_data, idx_use_out=None):
    """Help morphing.

    Equivalent to "data = (e[:, idx_use_data] * data)[idx_use_out]"
    but faster.
    """
    if len(idx_use_data) < e.shape[1]:
        if use_sparse:
            data = e[:, idx_use_data] * data
        else:
            # constructing a new sparse matrix is faster than sub-indexing
            # e[:, idx_use_data]!
            col, row = np.meshgrid(np.arange(data.shape[1]), idx_use_data)
            d_sparse = sparse.csr_matrix((data.ravel(),
                                          (row.ravel(), col.ravel())),
                                         shape=(e.shape[1], data.shape[1]))
            data = e * d_sparse
            data = np.asarray(data.todense())
    else:
        data = e * data

    # trim data
    if idx_use_out is not None:
        data = data[idx_use_out]
    return data


def _sparse_argmax_nnz_row(csr_mat):
    """Return index of the maximum non-zero index in each row."""
    n_rows = csr_mat.shape[0]
    idx = np.empty(n_rows, dtype=np.int)
    for k in range(n_rows):
        row = csr_mat[k].tocoo()
        idx[k] = row.col[np.argmax(row.data)]
    return idx


def _get_subject_sphere_tris(subject, subjects_dir):
    spheres = [os.path.join(subjects_dir, subject, 'surf',
                            xh + '.sphere.reg') for xh in ['lh', 'rh']]
    tris = [read_surface(s)[1] for s in spheres]
    return tris


###############################################################################
# Apply morph to source estimate
def _get_zooms_orig(morph):
    """Compute src zooms from morph zooms, morph shape and src shape."""
    # zooms_to = zooms_from / shape_to * shape_from for each spatial dimension
    return [mz / ss * ms for mz, ms, ss in
            zip(morph.morph_zooms, morph.morph_shape,
                morph.src_data['src_shape_full'])]


def _apply_morph_data(morph, stc_from):
    """Morph a source estimate from one subject to another."""
    if stc_from.subject is not None and stc_from.subject != morph.subject_from:
        raise ValueError('stc.subject (%s) != morph.subject_from (%s)'
                         % (stc_from.subject, morph.subject_from))
    if morph.kind == 'volume':
        from dipy.align.reslice import reslice

        # prepare data to be morphed
        img_to = _interpolate_data(stc_from, morph, mri_resolution=True,
                                   mri_space=True)

        # reslice to match morph
        img_to, img_to_affine = reslice(
            img_to.get_data(), morph.morph_affine, _get_zooms_orig(morph),
            morph.morph_zooms)

        # morph data
        for vol in range(img_to.shape[3]):
            img_to[:, :, :, vol] = morph.sdr_mapping.transform(
                morph.pre_sdr_affine.transform(img_to[:, :, :, vol]))

        # reshape to nvoxel x nvol
        img_to = img_to.reshape(-1, img_to.shape[3])

        vertices_to = np.where(img_to.sum(axis=1) != 0)[0]
        data = img_to[vertices_to]
        klass = VolSourceEstimate
    else:
        assert morph.kind == 'surface'
        morph_mat = morph.morph_mat
        vertices_to = morph.vertices_to
        for hemi, v1, v2 in zip(('left', 'right'),
                                morph.src_data, stc_from.vertices):
            if not np.array_equal(v1, v2):
                raise ValueError('vertices do not match between morph (%s) '
                                 'and stc (%s) for the %s hemisphere:\n%s\n%s'
                                 % (len(v1), len(v2), hemi, v1, v2))

        # select correct data - since vertices_to can have empty hemispheres,
        # the correct data needs to be selected in order to apply the morph_mat
        # correctly
        data = (stc_from.data
                if len(vertices_to[0]) != 0 and len(vertices_to[1]) != 0
                else (stc_from.lh_data
                      if len(vertices_to[0]) != 0
                      else stc_from.rh_data))

        # apply morph and return new morphed instance of (Vector)SourceEstimate
        if isinstance(stc_from, VectorSourceEstimate):
            # Morph the locations of the dipoles, but not their orientation
            n_verts, _, n_samples = stc_from.data.shape
            data = morph_mat * data.reshape(n_verts, 3 * n_samples)
            data = data.reshape(morph_mat.shape[0], 3, n_samples)
            klass = VectorSourceEstimate
        else:
            data = morph_mat * data
            klass = SourceEstimate
    stc_to = klass(data, vertices_to, stc_from.tmin, stc_from.tstep,
                   morph.subject_to)
    return stc_to
