# -*- coding: utf-8 -*-
"""
===========================
Cross-hemisphere comparison
===========================

This example illustrates how to visualize the difference between activity in
the left and the right hemisphere. The data from the right hemisphere is
mapped to the left hemisphere, and then the difference is plotted. For more
information see :class:`mne.SourceMorph`.
"""
# Author: Christian Brodbeck <christianbrodbeck@nyu.edu>
#
# License: BSD (3-clause)

import mne


data_dir = mne.datasets.sample.data_path()
subjects_dir = data_dir + '/subjects'
stc_path = data_dir + '/MEG/sample/sample_audvis-meg-eeg'

stc = mne.read_source_estimate(stc_path, 'sample')

# First, morph the data to fsaverage_sym, for which we have left_right
# registrations:
stc = stc.morph('fsaverage_sym', subjects_dir=subjects_dir, smooth=5)

# Compute a morph-matrix mapping the right to the left hemisphere. Use the
# vertices parameters to determine source and target hemisphere:
morph = mne.SourceMorph(subject_from='fsaverage_sym',
                        subject_to='fsaverage_sym',
                        spacing=[stc.vertices[0], []],
                        subjects_dir=subjects_dir, xhemi=True)
stc_m = morph(stc)

mm = morph.params['morph_mat']

# SourceEstimate on the left hemisphere:
stc_lh = mne.SourceEstimate(stc.lh_data, [stc.vertices[0], []], stc.tmin,
                            stc.tstep, stc.subject)
# SourceEstimate of the right hemisphere, morphed to the left:
stc_rh_on_lh = mne.SourceEstimate(mm * stc.rh_data, [stc.vertices[0], []],
                                  stc.tmin, stc.tstep, stc.subject)
# Since both STCs are now on the same hemisphere we can subtract them:
diff = stc_lh - stc_rh_on_lh

diff.plot(hemi='lh', subjects_dir=subjects_dir, initial_time=0.07,
          size=(800, 600))
