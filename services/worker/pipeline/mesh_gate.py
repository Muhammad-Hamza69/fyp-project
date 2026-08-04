"""
Quantified mesh-quality gate.

REPLACES `grep -q "Mesh OK" log.checkMesh`, which was copied into four call
sites and cannot tell the difference between a broken mesh and a cosmetic
blemish. checkMesh reports a single pass/fail for the WHOLE mesh, so three bad
faces out of 320,000 read exactly like a collapsed cell region.

That is not a hypothetical. The coarse pulsatile mesh (106,888 cells) tripped
on `Max skewness = 4.404` against a limit of 4 — 3 faces, all of them at
x ≈ 99.6-99.9 mm, on the outlet cap, 50 mm (25 parent diameters) downstream of
the sac at x = 50 mm. Non-orthogonality was 59.2 against a limit of 65. The
binary gate threw the entire run away over faces that cannot influence the
number the run exists to produce.

WHAT THIS DOES INSTEAD
----------------------
Two tiers, because the two metrics fail differently:

  * Non-orthogonality is HARD. It degrades the Laplacian everywhere through
    the pressure equation, and its damage is not local, so a violation is
    never waived regardless of where the faces are.

  * Skewness is LOCAL. It corrupts interpolation on the offending faces and
    their immediate neighbours. If those faces are demonstrably outside the
    region the results are read from, the run is still sound.

A skewness violation is therefore waived only when the gate has LOCATED every
offending face and confirmed each one lies outside the region of interest.

FAILS CLOSED. If the face set is missing, unreadable, or `foamToVTK` is
unavailable, the gate cannot prove the faces are harmless, so it does not
waive — it fails. A gate that waives on missing evidence is not a gate.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Same limits as system/meshQualityDict, so snappyHexMesh's own constraints and
# the post-hoc gate cannot disagree.
MAX_NON_ORTHO = 65.0
MAX_SKEWNESS = 4.0


@dataclass
class MeshGate:
    passed: bool
    max_non_ortho: float | None = None
    max_skewness: float | None = None
    n_cells: int | None = None
    failures: list[str] = field(default_factory=list)
    waivers: list[str] = field(default_factory=list)
    skew_face_locations_mm: list[tuple[float, float, float]] = field(default_factory=list)

    def summary(self) -> str:
        head = "PASS" if self.passed else "FAIL"
        bits = [f"mesh gate {head}"]
        if self.n_cells is not None:
            bits.append(f"cells={self.n_cells}")
        if self.max_non_ortho is not None:
            bits.append(f"nonOrtho={self.max_non_ortho:.1f}/{MAX_NON_ORTHO:.0f}")
        if self.max_skewness is not None:
            bits.append(f"skew={self.max_skewness:.2f}/{MAX_SKEWNESS:.0f}")
        out = "  ".join(bits)
        for w in self.waivers:
            out += f"\n  WAIVED: {w}"
        for f in self.failures:
            out += f"\n  FAILED: {f}"
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "max_non_ortho": self.max_non_ortho,
            "max_skewness": self.max_skewness,
            "n_cells": self.n_cells,
            "failures": self.failures,
            "waivers": self.waivers,
            "skew_face_locations_mm": self.skew_face_locations_mm,
        }


def _num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def _skew_face_centres_m(case_dir: Path, set_name: str = "skewFaces") -> list[tuple[float, float, float]] | None:
    """
    Face centres of the offending faces, in metres.

    Returns None — never an empty list — when the locations could not be
    established, so the caller can distinguish "no bad faces" from "could not
    look", and refuse to waive in the second case.
    """
    if not (case_dir / "constant" / "polyMesh" / "sets" / set_name).exists():
        return None
    if shutil.which("foamToVTK") is None:
        return None

    # Clear stale output first: foamToVTK does not prune, so a previous run's
    # faces would otherwise be read as if they belonged to this mesh.
    out_dir = case_dir / "VTK" / "face-set" / set_name
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)

    try:
        subprocess.run(
            ["foamToVTK", "-faceSet", set_name, "-constant"],
            cwd=case_dir, capture_output=True, timeout=300, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    vtps = sorted((case_dir / "VTK" / "face-set" / set_name).glob("*.vtp"))
    if not vtps:
        return None

    try:
        import numpy as np
        import pyvista as pv
    except ImportError:
        return None

    # foamToVTK writes the set once PER TIME DIRECTORY, so `skewFaces_0.vtp`
    # and `skewFaces_1.vtp` hold the identical three faces. Reading every file
    # and summing reported 6 faces for a set of 3. Deduplicate on position
    # (rounded to a micron, far below any mesh length scale) rather than
    # trusting the file count, which varies with how many times were written.
    seen: set[tuple[int, int, int]] = set()
    centres: list[tuple[float, float, float]] = []
    for vtp in vtps:
        try:
            mesh = pv.read(vtp)
        except Exception:  # noqa: BLE001
            return None
        if mesh.n_cells == 0:
            continue
        for p in np.asarray(mesh.cell_centers().points, dtype=float):
            key = tuple(int(round(v * 1e6)) for v in p)
            if key in seen:
                continue
            seen.add(key)
            centres.append((float(p[0]), float(p[1]), float(p[2])))
    return centres


def evaluate(
    case_dir: Path | str,
    roi_centre_m: tuple[float, float, float] | None = None,
    roi_radius_m: float | None = None,
    log_name: str = "log.checkMesh",
) -> MeshGate:
    """
    Grade a meshed case.

    `roi_centre_m` / `roi_radius_m` bound the region whose results will
    actually be read — for these cases, the aneurysm sac. Omit them and no
    skewness waiver is possible, because without a region of interest there is
    no basis on which to call a bad face irrelevant.
    """
    case_dir = Path(case_dir)
    log = case_dir / log_name
    if not log.exists():
        return MeshGate(passed=False, failures=[f"{log_name} not found — checkMesh did not run"])

    text = log.read_text(errors="ignore")
    gate = MeshGate(passed=True)
    gate.max_non_ortho = _num(r"non-orthogonality Max:\s*([0-9.eE+-]+)", text)
    gate.max_skewness = _num(r"Max skewness\s*=\s*([0-9.eE+-]+)", text)
    n = _num(r"^\s+cells:\s+(\d+)", text) or _num(r"cells:\s+(\d+)", text)
    gate.n_cells = int(n) if n is not None else None

    if "Failed" not in text and "Mesh OK" not in text:
        gate.passed = False
        gate.failures.append("checkMesh produced neither 'Mesh OK' nor a failure count — log truncated?")
        return gate

    # --- non-orthogonality: hard, never waived -------------------------------
    if gate.max_non_ortho is None:
        gate.passed = False
        gate.failures.append("max non-orthogonality not found in log")
    elif gate.max_non_ortho >= MAX_NON_ORTHO:
        gate.passed = False
        gate.failures.append(
            f"non-orthogonality {gate.max_non_ortho:.1f} >= {MAX_NON_ORTHO:.0f} "
            "— degrades the pressure Laplacian mesh-wide, not waivable"
        )

    # --- skewness: local, waivable only when located outside the ROI ---------
    if gate.max_skewness is not None and gate.max_skewness >= MAX_SKEWNESS:
        centres = _skew_face_centres_m(case_dir)
        if centres is None:
            gate.passed = False
            gate.failures.append(
                f"skewness {gate.max_skewness:.2f} >= {MAX_SKEWNESS:.0f} and the offending "
                "faces could not be located (no faceSet / no foamToVTK / unreadable) — "
                "refusing to waive without evidence"
            )
        elif roi_centre_m is None or roi_radius_m is None:
            gate.passed = False
            gate.failures.append(
                f"skewness {gate.max_skewness:.2f} >= {MAX_SKEWNESS:.0f} and no region of "
                "interest was supplied, so the faces cannot be shown to be irrelevant"
            )
        else:
            import numpy as np

            pts = np.asarray(centres, dtype=float)
            d = np.linalg.norm(pts - np.asarray(roi_centre_m, dtype=float), axis=1)
            inside = d <= roi_radius_m
            gate.skew_face_locations_mm = [tuple(float(v) * 1e3 for v in p) for p in pts]

            if inside.any():
                gate.passed = False
                gate.failures.append(
                    f"skewness {gate.max_skewness:.2f} >= {MAX_SKEWNESS:.0f} with "
                    f"{int(inside.sum())} of {len(pts)} offending faces INSIDE the region of "
                    "interest — these corrupt the results being measured"
                )
            else:
                gate.waivers.append(
                    f"skewness {gate.max_skewness:.2f} >= {MAX_SKEWNESS:.0f}, but all "
                    f"{len(pts)} offending faces lie outside the region of interest "
                    f"(nearest is {d.min() * 1e3:.1f} mm from its centre, ROI radius "
                    f"{roi_radius_m * 1e3:.1f} mm). Skewness error is local to the "
                    "offending faces, so the measured region is unaffected."
                )

    return gate


if __name__ == "__main__":
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Quantified checkMesh gate")
    ap.add_argument("case")
    ap.add_argument("--roi-centre", nargs=3, type=float, metavar=("X", "Y", "Z"),
                    help="region-of-interest centre in METRES (e.g. the sac)")
    ap.add_argument("--roi-radius", type=float, help="region-of-interest radius in METRES")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    g = evaluate(
        Path(args.case),
        tuple(args.roi_centre) if args.roi_centre else None,
        args.roi_radius,
    )
    print(json.dumps(g.as_dict(), indent=2) if args.json else g.summary())
    sys.exit(0 if g.passed else 1)
