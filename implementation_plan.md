# Implementation Plan - NeuroFlow CFD Local Python Pipeline

This implementation plan describes the technical setup and directory structure for the local Python clinical pipeline of "NeuroFlow CFD". The pipeline parses DICOM files, checks physiological constraints, and executes simulation logging placeholders.

## User Review Required

Please review the following structural design choices and adjustments:
1. **Directory Location**: Created inside a sub-workspace directory named `d:/fyp/neuroflow-local-pipeline/` to keep it organized and separate from the frontend dashboard.
2. **Third-party Library**: Relies on `pydicom` for medical imaging metadata extraction. If not present, we will install it using `pip` or verify its availability.
3. **Optional File Argument**: The command-line parser in `pipeline.py` will have an optional `--file` argument that defaults to `data/input/sample.dcm` to allow quick debugging without typing the full path every run.
4. **Mock DICOM Integrity**: The `create_mock_dicom.py` script will explicitly initialize the mandatory `FileMetaDataset` and define a valid `TransferSyntaxUID` to prevent DICOM corruption or parse errors in `pydicom`.

---

## Proposed Changes

### Configuration and Modules

#### [NEW] [config.py](file:///d:/fyp/neuroflow-local-pipeline/src/config.py)
A configuration module defining standard blood flow properties:
- `BLOOD_DENSITY` = 1060 (kg/m³)
- `BLOOD_VISCOSITY` = 0.0035 (Pa·s)

#### [NEW] [dicom_parser.py](file:///d:/fyp/neuroflow-local-pipeline/src/dicom_parser.py)
A parser module wrapping `pydicom` to extract:
- PatientID
- Modality
- StudyDate
- Rows
- Columns
- SliceThickness
- Uses Python `getattr(dataset, attribute, default)` patterns to protect execution on missing optional fields.

### Orchestration and Utilities

#### [NEW] [pipeline.py](file:///d:/fyp/neuroflow-local-pipeline/pipeline.py)
The CLI execution script. It will:
- Parse arguments for a target DICOM file (with a default fallback value of `data/input/sample.dcm` if `--file` is omitted).
- Print a formatted clinical logging header.
- Invoke the parser module to display image dimensions and demographics.
- Output log progress indicators for Step 2 (Automated AI Segmentation) and Step 3 (Post-Processing & CFD metrics extraction).

#### [NEW] [create_mock_dicom.py](file:///d:/fyp/neuroflow-local-pipeline/create_mock_dicom.py)
A utility script that creates a basic dummy DICOM file using `pydicom`'s dataset construction tools and saves it in `data/input/sample.dcm` to allow testing.
- **Mandatory Configuration**: The script will construct the `FileMetaDataset` object, populated with `MediaStorageSOPClassUID`, `MediaStorageSOPInstanceUID`, and `TransferSyntaxUID` (e.g., `1.2.840.10008.1.2.1` for Explicit VR Little Endian) to guarantee file compatibility with `pydicom.dcmread()`.

---

## Verification Plan

### Automated Tests
- Command to install `pydicom` if missing:
  ```powershell
  pip install pydicom
  ```
- Command to generate a dummy DICOM test file:
  ```powershell
  python create_mock_dicom.py
  ```
- Command to run the local pipeline (using the default fallback file path):
  ```powershell
  python pipeline.py
  ```
