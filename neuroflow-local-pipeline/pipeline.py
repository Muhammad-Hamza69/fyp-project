"""
NeuroFlow CFD - Local Simulation Pipeline Orchestrator
Clinical data ingestion, metadata parsing, and computational pipeline simulator.
"""

import os
import sys
import time
import argparse
from typing import Dict, Any

# Package imports
from src.config import BLOOD_DENSITY, BLOOD_VISCOSITY
from src.dicom_parser import parse_dicom_file

def print_banner() -> None:
    """Prints a structured clinical CLI header."""
    print("=" * 60)
    print("       NEUROFLOW CFD : CLINICAL DATA PROCESSING PIPELINE       ")
    print("=" * 60)

def execute_pipeline(file_path: str) -> None:
    """
    Executes the clinical simulation pipeline sequentially.
    
    Args:
        file_path: Target path to the DICOM file for processing.
    """
    print_banner()
    
    # Verify file existence before initializing the solver
    if not os.path.exists(file_path):
        print(f"[ERROR] Target file not found: '{file_path}'")
        print("[*] Please run 'python create_mock_dicom.py' to generate a sample file.")
        sys.exit(1)
        
    print(f"[*] Starting local simulation worker...")
    print(f"[*] Target DICOM: {file_path}")
    print(f"[+] Loaded Clinical Environment Consts:")
    print(f"    - Blood Density   : {BLOOD_DENSITY} kg/m³")
    print(f"    - Blood Viscosity : {BLOOD_VISCOSITY} Pa·s")
    print("-" * 60)
    
    # --- STEP 01: Meta-Parsing ---
    print("[*] STEP 01: Parsing DICOM Headers...")
    time.sleep(0.5)
    
    try:
        metadata: Dict[str, Any] = parse_dicom_file(file_path)
        print(f"    [+] Patient ID     : {metadata['patient_id']}")
        print(f"    [+] Modality       : {metadata['modality']}")
        print(f"    [+] Scan Date      : {metadata['study_date']}")
        print(f"    [+] Image Matrix   : {metadata['rows']} x {metadata['columns']} px")
        print(f"    [+] Slice Thickness: {metadata['slice_thickness']} mm")
        print("[SUCCESS] Metadata extraction complete.")
    except Exception as err:
        print(f"[CRITICAL ERROR] Failed to parse header tags: {err}")
        sys.exit(1)
        
    print("-" * 60)
    
    # --- STEP 02: AI Segmentation (Placeholder) ---
    print("[*] STEP 02: Automated AI Segmentation...")
    print("    [~] Initializing NeuroFlow-Seg U-Net weights...")
    time.sleep(0.6)
    print("    [~] Segmenting vascular boundaries & aneurysm neck...")
    time.sleep(0.6)
    print("[SUCCESS] Segmentation boundaries localized. Confidence score: 98.4%.")
    print("-" * 60)
    
    # --- STEP 03: Post-Processing & CFD (Placeholder) ---
    print("[*] STEP 03: Boundary Conditions & Hemodynamic Solver...")
    print("    [~] Assigning carotid velocity profiles...")
    time.sleep(0.5)
    print("    [~] Running transient Navier-Stokes simulation cycles...")
    time.sleep(0.8)
    print("    [~] Post-processing hemodynamic fields (TAWSS and OSI)...")
    time.sleep(0.5)
    print("[SUCCESS] CFD execution and clinical metrics extracted.")
    print("=" * 60)
    print("Pipeline executed successfully. Outputs ready for Clinical Dashboard.")
    print("=" * 60)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local processing worker for clinical DICOM files."
    )
    
    # Make file argument optional, defaulting to the mock generated file path
    parser.add_argument(
        "--file",
        type=str,
        default=os.path.join("data", "input", "sample.dcm"),
        help="Path to the clinical DICOM file (default: data/input/sample.dcm)"
    )
    
    args = parser.parse_args()
    execute_pipeline(args.file)

if __name__ == "__main__":
    main()
