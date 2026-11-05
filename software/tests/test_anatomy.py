"""Supported anatomy formats and their physical coordinate mapping."""
from pathlib import Path
import tempfile
import unittest

import nibabel as nib
import numpy as np

from opendrive.anatomy import AnatomyLoader


class AnatomyFormatTests(unittest.TestCase):
    def test_nifti_and_compressed_nifti_keep_physical_geometry(self):
        data = np.indices((7, 8, 9)).sum(axis=0).astype(np.float32)
        affine = np.diag([.5, .75, 1.25, 1.])
        affine[:3, 3] = [-3, 4, -5]
        with tempfile.TemporaryDirectory() as folder:
            for suffix in ('.nii', '.nii.gz'):
                path = Path(folder) / f'anatomy{suffix}'
                nib.save(nib.Nifti1Image(data, affine), path)
                volume = AnatomyLoader.load(path)
                np.testing.assert_array_equal(volume.data, data)
                np.testing.assert_allclose(volume.affine, affine)

    def test_dicom_files_and_directories_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'image.dcm'
            path.write_bytes(b'unsupported')
            for source in (path, Path(folder)):
                with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'NIfTI'):
                    AnatomyLoader.load(source)
