"""
Finalise a set of solved cases into everything the deployed app needs.

One command turns solved OpenFOAM cases into:
  1. per-case coloured GLB meshes  (models/{id}.glb, {id}-osi.glb)
  2. the static dashboard export   (real-cfd-patients.json)
  3. database rows                 (Patient / Study / Run / CFDResult / AIResult)
  4. a clinical PDF per case       (docs/reports/{id}.pdf)

Written as a script rather than run by hand because the steps must stay
consistent with each other: the JSON, the database and the mesh all have to
describe the same run, and doing that manually across three cases is how they
drift apart.

    python finalize.py --cases ~/cases/cohort/PT-2026-0101 ~/cases/cohort/PT-2026-0102
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE / "pipeline"))
sys.path.insert(0, str(_HERE.parent / "api"))

REPO = _HERE.parent.parent
MODELS_DIR = REPO / "models"
REPORTS_DIR = REPO / "docs" / "reports"
PATIENTS_JSON = REPO / "real-cfd-patients.json"

# Demographics per case. Kept here rather than inferred so the PHASES score is
# driven by declared clinical facts rather than by anything the solver produced.
DEMOGRAPHICS: dict[str, dict[str, Any]] = {
    "PT-2026-0101": {"age": 64, "hypertension": True, "earlierSAH": False,
                     "population": "Other", "site": "MCA"},
    "PT-2026-0102": {"age": 71, "hypertension": True, "earlierSAH": False,
                     "population": "Other", "site": "ACOM_PCOM_POST"},
    "PT-2026-0103": {"age": 49, "hypertension": False, "earlierSAH": False,
                     "population": "Other", "site": "ICA"},
}


def process_case(case_dir: Path, patient_id: str, faces: int) -> dict[str, Any]:
    from export_patient import build_patient          # type: ignore
    from export_mesh import build as build_mesh       # type: ignore

    result: dict[str, Any] = {"id": patient_id, "case": str(case_dir)}

    record = build_patient(case_dir, patient_id,
                           DEMOGRAPHICS.get(patient_id, DEMOGRAPHICS["PT-2026-0101"]))
    result["record"] = record

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for mode, suffix in (("TAWSS", ""), ("OSI", "-osi")):
        try:
            meta = build_mesh(case_dir, MODELS_DIR / f"{patient_id}{suffix}.glb",
                              mode=mode, target_faces=faces)
            result[f"mesh_{mode.lower()}"] = meta["bytes"]
        except Exception as exc:  # noqa: BLE001
            result[f"mesh_{mode.lower()}_error"] = str(exc)

    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--faces", type=int, default=40000)
    ap.add_argument("--skip-db", action="store_true")
    ap.add_argument("--skip-reports", action="store_true")
    args = ap.parse_args()

    records, summary = [], []
    for c in args.cases:
        case_dir = Path(c).expanduser()
        if not case_dir.exists():
            print(f"!! missing case {case_dir}", flush=True)
            continue
        pid = case_dir.name
        print(f"[{pid}] processing…", flush=True)
        try:
            out = process_case(case_dir, pid, args.faces)
            records.append(out["record"])
            r = out["record"]
            dome = r["zones"][3]
            summary.append(
                f"  {pid}: dome TAWSS {dome['tawss']:.3f} Pa | "
                f"CRI {r['riskBreakdown']['composite']} ({r['riskTier']}) | "
                f"mesh {out.get('mesh_tawss', 0)/1e6:.2f} MB"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"!! {pid} failed: {exc.__class__.__name__}: {exc}", flush=True)

    if not records:
        print("no cases processed")
        return 1

    PATIENTS_JSON.write_text(json.dumps({
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "pipeline": "NeuroFlow CFD worker — geometry → snappyHexMesh → OpenFOAM → hemodynamic engine",
        "patients": records,
    }, indent=2))
    print(f"\nwrote {PATIENTS_JSON} ({len(records)} case(s))")

    if not args.skip_reports:
        try:
            from report import generate_report          # type: ignore
            from risk_model import extract_features, predict  # type: ignore
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            for r in records:
                ai = None
                try:
                    feats = extract_features(
                        {"zones": [{"id": "dome", "tawss": r["zones"][3]["tawss"],
                                    "osi": r["zones"][3]["osi"],
                                    "rrt": r["hemodynamics"].get("rrt", 0),
                                    "ecap": r["hemodynamics"].get("ecap", 0)}],
                         "nwss": r["hemodynamics"].get("nwss", 0),
                         "lsar_relative": r["hemodynamics"].get("lsarRelative", 0)},
                        r["morphology"], r["demographics"])
                    ai = predict(feats, _HERE / "models")
                except Exception:
                    pass
                generate_report(r, REPORTS_DIR / f"{r['id']}.pdf", ai)
            print(f"wrote {len(records)} report(s) to {REPORTS_DIR}")
        except Exception as exc:  # noqa: BLE001
            print(f"(reports skipped: {exc})")

    if not args.skip_db:
        try:
            from db import get_session, init_db        # type: ignore
            from ingest import ingest_record           # type: ignore
            init_db()
            s = get_session()
            try:
                for r in records:
                    ingest_record(r, s)
            finally:
                s.close()
            print(f"ingested {len(records)} case(s) into the database")
        except Exception as exc:  # noqa: BLE001
            print(f"(database ingest skipped: {exc})")

    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
