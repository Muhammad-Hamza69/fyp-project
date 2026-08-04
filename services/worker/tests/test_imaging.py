"""
Imaging pipeline tests: DICOM validation, quality assessment, segmentation.

The validation tests matter because the module's job is to REJECT unsuitable
studies. Every downstream clinical threshold assumes brain vasculature acquired
on MR or CT; a chest CT that slips through would be meshed, solved, and scored
against cerebral-aneurysm criteria without a single error being raised.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import SimpleITK as sitk

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from imaging import (  # noqa: E402
    SUPPORTED_MODALITIES, assess_quality, preprocess, segmentation_metrics,
    validate_dicom,
)


def _volume(shape=(40, 60, 60), spacing=(0.5, 0.5, 0.5), seed=0) -> sitk.Image:
    rng = np.random.default_rng(seed)
    arr = rng.normal(100, 5, shape).astype(np.float32)
    # bright tube through the middle => something for SNR/CNR to measure
    arr[:, 25:35, 25:35] += 400
    img = sitk.GetImageFromArray(arr)
    img.SetSpacing(spacing)
    return img


class TestDicomValidation:
    def test_rejects_nondicom(self, tmp_path):
        f = tmp_path / "not_a_scan.dcm"
        f.write_text("PATIENT_ID = PT-2025-0061\nMODALITY = MR\n")
        v = validate_dicom(f)
        assert v.valid is False
        assert v.reasons, "a text stub must produce a stated reason for rejection"

    def test_rejects_empty_directory(self, tmp_path):
        v = validate_dicom(tmp_path)
        assert v.valid is False
        assert any("no DICOM" in r for r in v.reasons)

    def test_supported_modalities_cover_the_clinical_set(self):
        # CTA/MRA are the acquisitions this pipeline is built for.
        assert {"MR", "CT"} <= SUPPORTED_MODALITIES

    def test_real_series_passes(self, tmp_path):
        """A generated phantom series must satisfy every validation rule."""
        from phantom import PhantomConfig, generate, write_dicom_series  # noqa: E402
        from geometry import AneurysmGeometry  # noqa: E402

        img, _ = generate(AneurysmGeometry(), PhantomConfig(spacing_mm=0.8, snr=14))
        d = tmp_path / "dicom"
        write_dicom_series(img, d, "PT-TEST-0001")

        v = validate_dicom(d)
        assert v.valid is True, f"unexpected rejection: {v.reasons}"
        assert v.modality == "MR"
        assert v.is_brain is True
        assert v.n_slices >= 20


class TestQualityAssessment:
    def test_snr_tracks_injected_noise(self):
        """
        A quality metric that does not respond to image quality is decoration.
        The noisier volume must score lower — this is what makes the module's
        agreement with the phantom's configured SNR meaningful rather than
        coincidental.
        """
        clean = assess_quality(_volume(seed=1))
        rng = np.random.default_rng(2)
        arr = sitk.GetArrayFromImage(_volume(seed=1))
        noisy_img = sitk.GetImageFromArray(arr + rng.normal(0, 60, arr.shape).astype(np.float32))
        noisy_img.SetSpacing((0.5, 0.5, 0.5))
        noisy = assess_quality(noisy_img)
        assert clean.snr > noisy.snr
        assert clean.score > noisy.score

    def test_flags_anisotropic_voxels(self):
        q = assess_quality(_volume(spacing=(0.4, 0.4, 3.0)))
        assert q.is_isotropic is False
        assert any("anisotropic" in f for f in q.flags)

    def test_score_bounded(self):
        for sp in ((0.4, 0.4, 0.4), (0.4, 0.4, 4.0)):
            q = assess_quality(_volume(spacing=sp))
            assert 0.0 <= q.score <= 1.0


class TestPreprocessing:
    def test_resamples_to_isotropic(self):
        """
        Multi-scale Hessian vesselness assumes isotropic voxels; anisotropic
        input biases vessel-scale estimates along the thick axis.
        """
        out = preprocess(_volume(spacing=(0.4, 0.4, 2.0)), target_spacing=0.5)
        sp = out.GetSpacing()
        assert max(sp) / min(sp) == pytest.approx(1.0, abs=1e-6)

    def test_normalises_intensity(self):
        arr = sitk.GetArrayFromImage(preprocess(_volume()))
        assert arr.min() >= -1e-6
        assert arr.max() <= 1.0 + 1e-6


class TestSegmentationMetrics:
    def _mask(self, arr):
        img = sitk.GetImageFromArray(arr.astype(np.uint8))
        img.SetSpacing((1.0, 1.0, 1.0))
        return img

    def test_identical_masks_give_dice_one(self):
        a = np.zeros((20, 20, 20), np.uint8); a[5:15, 5:15, 5:15] = 1
        m = segmentation_metrics(self._mask(a), self._mask(a))
        assert m.dice == pytest.approx(1.0)

    def test_disjoint_masks_give_dice_zero(self):
        a = np.zeros((20, 20, 20), np.uint8); a[0:5, 0:5, 0:5] = 1
        b = np.zeros((20, 20, 20), np.uint8); b[15:20, 15:20, 15:20] = 1
        m = segmentation_metrics(self._mask(a), self._mask(b))
        assert m.dice == pytest.approx(0.0)

    def test_half_overlap_gives_expected_dice(self):
        a = np.zeros((20, 20, 20), np.uint8); a[0:10, :, :] = 1
        b = np.zeros((20, 20, 20), np.uint8); b[5:15, :, :] = 1
        # |A|=|B|=4000, |A n B|=2000 -> 2*2000/8000 = 0.5
        m = segmentation_metrics(self._mask(a), self._mask(b))
        assert m.dice == pytest.approx(0.5, rel=1e-6)

    def test_counts_connected_components(self):
        a = np.zeros((30, 30, 30), np.uint8)
        a[2:10, 2:10, 2:10] = 1
        a[20:28, 20:28, 20:28] = 1
        m = segmentation_metrics(self._mask(a))
        assert m.n_components == 2
        assert m.largest_component_fraction == pytest.approx(0.5, rel=1e-6)
