"""
Synthetic TOF-MRA phantom generator.

WHAT THIS IS FOR, AND WHAT IT IS NOT FOR
----------------------------------------
Legitimate uses:
  * producing genuine multi-slice DICOM so the ingestion path can be exercised
    (the three .dcm files in the repository root are ASCII text stubs that
    pydicom rejects outright)
  * end-to-end pipeline and CI testing without a 5 GB dataset download
  * verifying the segmentation code runs and is self-consistent

What it must NOT be used for: **claiming segmentation accuracy.** The mask is
generated from the same geometry the image is rendered from, so scoring a
segmentation against it is circular. A Dice of 0.9 here says the code works on
clean synthetic data; it says nothing about real TOF-MRA, where flow voids,
partial-volume blur at sub-millimetre branches, coil-sensitivity gradients and
turbulent dephasing at the aneurysm neck are what actually make the problem
hard. Any accuracy claim must come from annotated clinical data.

The noise model is Rician rather than Gaussian because MRI magnitude images are
Rician-distributed — Gaussian noise on a magnitude image is simply the wrong
statistics, and it makes the segmentation look better than it should.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import SimpleITK as sitk

from geometry import AneurysmGeometry, _sdf_capped_cylinder


@dataclass
class PhantomConfig:
    spacing_mm: float = 0.4          # typical TOF-MRA in-plane resolution
    snr: float = 14.0                # clinically representative
    bias_strength: float = 0.18      # coil sensitivity falloff
    partial_volume_mm: float = 0.35  # blur at the vessel boundary
    seed: int = 20260804


def _rician_noise(clean: np.ndarray, sigma: float, rng: np.random.Generator) -> np.ndarray:
    """
    MRI magnitude images are Rician: the magnitude of a complex signal with
    independent Gaussian noise on each channel. At high SNR this approaches a
    Gaussian, but in the dark background — exactly where a vesselness filter
    decides what is *not* a vessel — the difference is large.
    """
    real = clean + rng.normal(0.0, sigma, clean.shape)
    imag = rng.normal(0.0, sigma, clean.shape)
    return np.sqrt(real**2 + imag**2)


def generate(
    geom: AneurysmGeometry | None = None,
    cfg: PhantomConfig | None = None,
) -> tuple[sitk.Image, sitk.Image]:
    """Return (mra_image, ground_truth_mask) as SimpleITK images in mm."""
    geom = geom or AneurysmGeometry()
    cfg = cfg or PhantomConfig()
    rng = np.random.default_rng(cfg.seed)

    sp = cfg.spacing_mm / 1000.0     # metres, to match the geometry module
    pad = 0.006
    x = np.arange(-pad, geom.total_length + pad, sp)
    y = np.arange(-geom.parent_radius - pad, geom.sac_centre_y + geom.sac_radius + pad, sp)
    z = np.arange(-geom.sac_radius - pad, geom.sac_radius + pad, sp)
    gz, gy, gx = np.meshgrid(z, y, x, indexing="ij")   # (z, y, x) for SimpleITK

    d_parent = _sdf_capped_cylinder(gx, gy, gz, 0.0, geom.total_length, geom.parent_radius)
    d_sac = np.sqrt((gx - geom.sac_centre_x) ** 2
                    + (gy - geom.sac_centre_y) ** 2
                    + gz**2) - geom.sac_radius
    sdf = np.minimum(d_parent, d_sac)

    truth = (sdf <= 0).astype(np.uint8)

    # Soft boundary: a real acquisition never produces a step edge, and a step
    # edge makes the segmentation task artificially easy.
    pv = cfg.partial_volume_mm / 1000.0
    clean = 1.0 / (1.0 + np.exp(sdf / max(pv, 1e-6)))

    # Static tissue background so the filter has something to reject.
    clean = 0.12 + 0.88 * clean

    # Multiplicative bias field (receive-coil sensitivity), smooth and low-order.
    nz, ny, nx = clean.shape
    zz = np.linspace(-1, 1, nz)[:, None, None]
    yy = np.linspace(-1, 1, ny)[None, :, None]
    xx = np.linspace(-1, 1, nx)[None, None, :]
    bias = 1.0 + cfg.bias_strength * (0.6 * xx + 0.5 * yy - 0.4 * zz + 0.3 * xx * yy)
    clean = clean * bias

    sigma = float(clean[truth > 0].mean()) / cfg.snr
    noisy = _rician_noise(clean, sigma, rng)

    noisy = (noisy - noisy.min()) / (noisy.max() - noisy.min() + 1e-12)
    vol = (noisy * 4095).astype(np.int16)          # 12-bit, as MR typically is

    img = sitk.GetImageFromArray(vol)
    img.SetSpacing((cfg.spacing_mm,) * 3)
    img.SetOrigin((0.0, 0.0, 0.0))

    gt = sitk.GetImageFromArray(truth)
    gt.CopyInformation(img)
    return img, gt


# Wording a radiology order would actually use, so the parser matches on
# clinical language rather than on our own internal enum.
SITE_TERMS = {
    "ICA": "internal carotid artery",
    "MCA": "middle cerebral artery",
    "ACOM_PCOM_POST": "anterior communicating artery",
}


def write_dicom_series(
    img: sitk.Image, out_dir: Path, patient_id: str, series_desc: str = "TOF MRA BRAIN",
    clinical: dict[str, Any] | None = None,
) -> list[Path]:
    """
    Write a genuine, standards-compliant multi-slice DICOM series.

    Tags are set so the validation module's brain/modality checks pass on real
    metadata rather than on a filename regex: BodyPartExamined=BRAIN,
    Modality=MR, plus consistent Study/Series UIDs and slice positions.
    """
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    c = clinical or {}
    arr = sitk.GetArrayFromImage(img).astype(np.uint16)
    sx, sy, sz = img.GetSpacing()
    study_uid, series_uid = generate_uid(), generate_uid()
    now = datetime.now()
    written: list[Path] = []

    for i in range(arr.shape[0]):
        fm = FileMetaDataset()
        fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.4"   # MR Image Storage
        fm.MediaStorageSOPInstanceUID = generate_uid()
        fm.TransferSyntaxUID = ExplicitVRLittleEndian
        fm.ImplementationClassUID = generate_uid()

        ds = Dataset()
        ds.file_meta = fm
        ds.is_little_endian = True
        ds.is_implicit_VR = False

        ds.SOPClassUID = fm.MediaStorageSOPClassUID
        ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid

        ds.PatientID = patient_id
        ds.PatientName = "ANONYMISED^PHANTOM"
        ds.PatientSex = c.get("sex", "O")

        # --- clinical history, in the standard tags that carry it -----------
        #
        # PHASES needs age, hypertension, prior SAH, population and aneurysm
        # site. DICOM has real places for all of them, and a scan that reaches a
        # reporting workstation normally carries them because they came in on
        # the order. Writing them here means the dashboard reads the history
        # from the FILE rather than defaulting it — defaulting hypertension to
        # "No" scores it zero and manufactures a low rupture risk out of nothing.
        #
        #   (0010,1010) PatientAge                     "064Y"
        #   (0010,2160) EthnicGroup                    population term
        #   (0010,21B0) AdditionalPatientHistory       free text, parsed for
        #                                              hypertension / prior SAH
        #   (0008,1080) AdmittingDiagnosesDescription  names the vessel
        if c.get("age"):
            ds.PatientAge = f"{int(c['age']):03d}Y"
        if c.get("population"):
            ds.EthnicGroup = str(c["population"])

        history = []
        if c.get("hypertension") is not None:
            history.append("HYPERTENSION" if c["hypertension"] else "NO HYPERTENSION")
        if c.get("earlier_sah") is not None:
            history.append("PRIOR SAH" if c["earlier_sah"] else "NO PRIOR SAH")
        if history:
            ds.AdditionalPatientHistory = "; ".join(history)

        if c.get("site"):
            ds.AdmittingDiagnosesDescription = (
                f"Unruptured intracranial aneurysm, {SITE_TERMS.get(c['site'], c['site'])}"
            )
        ds.StudyDate = now.strftime("%Y%m%d")
        ds.StudyTime = now.strftime("%H%M%S")
        ds.Modality = "MR"
        ds.BodyPartExamined = "BRAIN"
        ds.SeriesDescription = series_desc
        ds.ProtocolName = "3D TOF MRA CIRCLE OF WILLIS"
        ds.Manufacturer = "NeuroFlow Synthetic"
        ds.ManufacturerModelName = "Phantom Generator v1"
        ds.ScanningSequence = "GR"
        ds.SequenceVariant = "SP"
        ds.MRAcquisitionType = "3D"

        ds.Rows, ds.Columns = int(arr.shape[1]), int(arr.shape[2])
        ds.PixelSpacing = [float(sy), float(sx)]
        ds.SliceThickness = float(sz)
        ds.SpacingBetweenSlices = float(sz)
        ds.InstanceNumber = i + 1
        ds.ImagePositionPatient = [0.0, 0.0, float(i * sz)]
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.SliceLocation = float(i * sz)

        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 12
        ds.HighBit = 11
        ds.PixelRepresentation = 0
        ds.PixelData = arr[i].tobytes()

        p = out_dir / f"slice_{i:04d}.dcm"
        ds.save_as(str(p), enforce_file_format=True)
        written.append(p)

    return written


if __name__ == "__main__":
    import argparse, json

    ap = argparse.ArgumentParser(description="Generate a synthetic TOF-MRA phantom")
    ap.add_argument("--out", default="~/data/phantom")
    ap.add_argument("--id", default="PT-2026-0201")
    ap.add_argument("--sac-radius-mm", type=float, default=4.0)
    ap.add_argument("--snr", type=float, default=14.0)
    args = ap.parse_args()

    out = Path(args.out).expanduser()
    geom = AneurysmGeometry(sac_radius=args.sac_radius_mm / 1000.0)
    img, gt = generate(geom, PhantomConfig(snr=args.snr))

    files = write_dicom_series(img, out / "dicom", args.id)
    sitk.WriteImage(gt, str(out / "ground_truth.nii.gz"))

    print(json.dumps({
        "dicom_dir": str(out / "dicom"),
        "slices": len(files),
        "size": list(img.GetSize()),
        "spacing_mm": list(img.GetSpacing()),
        "ground_truth": str(out / "ground_truth.nii.gz"),
        "vessel_voxels": int(sitk.GetArrayFromImage(gt).sum()),
    }, indent=2))
