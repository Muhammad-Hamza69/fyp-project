"""
DICOM Metadata Parser Module
Extracts clinical metadata from DICOM files using pydicom.
"""

from typing import Dict, Any
import pydicom

def parse_dicom_file(file_path: str) -> Dict[str, Any]:
    """
    Safely reads a DICOM file from file_path and extracts demographic 
    and matrix parameters. Uses getattr() to prevent crashing on missing fields.
    
    Args:
        file_path: The absolute or relative path to the target DICOM file.
        
    Returns:
        A dictionary containing extracted clinical fields.
    """
    # Parse the dataset using dcmread
    dataset = pydicom.dcmread(file_path)
    
    # Safely extract metadata using default fallback properties
    metadata: Dict[str, Any] = {
        "patient_id": str(getattr(dataset, "PatientID", "ANONYMOUS")),
        "modality": str(getattr(dataset, "Modality", "UNKNOWN")),
        "study_date": str(getattr(dataset, "StudyDate", "UNKNOWN")),
        "rows": int(getattr(dataset, "Rows", 0)),
        "columns": int(getattr(dataset, "Columns", 0)),
        "slice_thickness": float(getattr(dataset, "SliceThickness", 0.0))
    }
    
    return metadata
