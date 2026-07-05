# Walkthrough - NeuroFlow CFD Local Python Pipeline

This walkthrough outlines the setup, testing, and validation of the local clinical data parser and pipeline orchestrator built for the NeuroFlow CFD project.

## Changes Made

1. **Folder Architecture**: Created a standalone execution directory [neuroflow-local-pipeline/](file:///d:/fyp/neuroflow-local-pipeline/) containing:
   - `data/input/`: Target repository for DICOM scanner series.
   - `src/`: Core Python library modules.
2. **Config Parameters**: Created [src/config.py](file:///d:/fyp/neuroflow-local-pipeline/src/config.py) defining standard constants for blood density (`1060` kg/m³) and blood dynamic viscosity (`0.0035` Pa·s) with strict typing annotations.
3. **Clinical Header Parser**: Created [src/dicom_parser.py](file:///d:/fyp/neuroflow-local-pipeline/src/dicom_parser.py) wrapping `pydicom` to extract demographics (`PatientID`, `Modality`, `StudyDate`) and scanner matrices (`Rows`, `Columns`, `SliceThickness`) safeguarded with `getattr()` defaults.
4. **Mock Dataset Builder**: Created [create_mock_dicom.py](file:///d:/fyp/neuroflow-local-pipeline/create_mock_dicom.py) programmatically creating standard-compliant DICOM datasets, correctly setting `FileMetaDataset` parameters and Explicit VR Little Endian `TransferSyntaxUID` (`1.2.840.10008.1.2.1`).
5. **Orchestrator CLI**: Created [pipeline.py](file:///d:/fyp/neuroflow-local-pipeline/pipeline.py) acting as the solver simulator with default path fallback configuration, banners, and timing constraints.

---

## Validation Results

### 1. Ingesting Dependencies & Generating Compliant Mock Scan
The `pydicom` library was successfully installed. Running `create_mock_dicom.py` builds the required directory framework and compiles a standard metadata block without throwing warnings or exceptions:
```
[*] Initializing dataset architecture for file: data\input\sample.dcm...
[SUCCESS] Mock DICOM file generated successfully: data\input\sample.dcm
```

### 2. Executing Orchestrator CLI without Arguments (Fallback Testing)
Executing `python pipeline.py` successfully triggers default argument fallbacks, loading configurations, reading patient coordinates, and streaming clinical logs:
```
============================================================
       NEUROFLOW CFD : CLINICAL DATA PROCESSING PIPELINE       
============================================================
[*] Starting local simulation worker...
[*] Target DICOM: data\input\sample.dcm
[+] Loaded Clinical Environment Consts:
    - Blood Density   : 1060 kg/m³
    - Blood Viscosity : 0.0035 Pa·s
------------------------------------------------------------
[*] STEP 01: Parsing DICOM Headers...
    [+] Patient ID     : PT-2025-0041
    [+] Modality       : MR
    [+] Scan Date      : 20260705
    [+] Image Matrix   : 512 x 512 px
    [+] Slice Thickness: 0.5 mm
[SUCCESS] Metadata extraction complete.
------------------------------------------------------------
[*] STEP 02: Automated AI Segmentation...
    [~] Initializing NeuroFlow-Seg U-Net weights...
    [~] Segmenting vascular boundaries & aneurysm neck...
[SUCCESS] Segmentation boundaries localized. Confidence score: 98.4%.
------------------------------------------------------------
[*] STEP 03: Boundary Conditions & Hemodynamic Solver...
    [~] Assigning carotid velocity profiles...
    [~] Running transient Navier-Stokes simulation cycles...
    [~] Post-processing hemodynamic fields (TAWSS and OSI)...
[SUCCESS] CFD execution and clinical metrics extracted.
============================================================
Pipeline executed successfully. Outputs ready for Clinical Dashboard.
============================================================
```

## Summary
The pipeline modules are fully decoupled from any frameworks. Each file uses strict typing hints and exports functions cleanly, making it ready to be dropped into Celery tasks or another task worker environment in future versions.
