"""
Mock DICOM Dataset Generator
Creates a valid mock DICOM file with mandatory clinical headers
to test the NeuroFlow CFD Local Python Pipeline.
"""

import os
import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

def generate_mock_dicom() -> None:
    """
    Creates a standard-compliant sample DICOM file under data/input/sample.dcm
    with appropriate metadata for diagnostic pipeline simulations.
    """
    # Establish target output directory
    output_dir = os.path.join("data", "input")
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, "sample.dcm")
    
    print(f"[*] Initializing dataset architecture for file: {filepath}...")
    
    # 1. Initialize Mandatory File Metadata Dataset (pydicom dataset headers)
    file_meta = Dataset()
    
    # SOP Class: MR Image Storage UID
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.ImplementationClassUID = "1.2.840.10008.1.2.3.4"
    
    # MANDATORY TransferSyntaxUID: Explicit VR Little Endian ('1.2.840.10008.1.2.1')
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    
    # 2. Construct the FileDataset object
    # Includes standard 128-byte preamble (padding bytes)
    ds = FileDataset(filepath, {}, file_meta=file_meta, preamble=b"\0" * 128)
    
    # Configure endianness constraints for Explicit VR format
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    
    # 3. Populate Required Demographic and Imaging Tags
    ds.PatientID = "PT-2025-0041"
    ds.PatientName = "ANONYMOUS^CASE^ONE"
    ds.Modality = "MR"
    ds.StudyDate = "20260705"
    ds.Rows = 512
    ds.Columns = 512
    ds.SliceThickness = 0.5
    
    # Set matching Instance UIDs
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    
    # Write dataset to file
    ds.save_as(filepath, write_like_original=False)
    print(f"[SUCCESS] Mock DICOM file generated successfully: {filepath}")

if __name__ == "__main__":
    generate_mock_dicom()
