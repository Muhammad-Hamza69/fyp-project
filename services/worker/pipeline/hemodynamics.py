"""
Hemodynamic biomarker extraction from a solved OpenFOAM case.

Reads cycle-averaged wall shear stress off the named wall patches and derives
the clinical metrics the dashboard reports: TAWSS, OSI, RRT, ECAP, LSAR, NWSS.

THREE TRAPS ARE HANDLED HERE, and each one silently produces plausible-looking
but wrong numbers if missed:

1. UNITS. OpenFOAM's incompressible solvers are kinematic, so the
   `wallShearStress` function object writes m^2/s^2, NOT Pascals. Everything
   must be multiplied by rho = 1060. Miss this and every TAWSS is ~1000x too
   small, so every case trips the "< 0.4 Pa" low-shear alert and the dashboard
   confidently reports universal critical risk.

2. TAWSS IS NOT |mean(tau)|. `fieldAverage` applied to the wallShearStress
   VECTOR yields |mean(tau_vec)| -- which is the OSI *numerator*, not TAWSS.
   TAWSS is mean(|tau|), the average of the magnitude, which is why controlDict
   computes `mag` BEFORE `fieldAverage`. The two coincide only in perfectly
   unidirectional flow; inside an aneurysm sac they differ substantially, and
   using the wrong one drives OSI towards 0 exactly where it should be high.

3. AREA WEIGHTING. Patch faces vary in size by an order of magnitude after
   snappy refinement, so a plain numpy mean over face values over-weights the
   small cells. Every zone statistic here is area-weighted.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np
import pyvista as pv

# Blood density, kg/m^3. Also THE kinematic -> Pa conversion factor.
RHO = 1060.0

# Clinical thresholds (mirrors packages/shared/src/thresholds.ts).
TAWSS_LOW_PA = 0.4
OSI_HIGH = 0.2
RRT_HIGH = 3.0
ECAP_HIGH = 1.0

# Guards, identical to the TypeScript side so client and server agree.
DIVISION_FLOOR = 0.02

WALL_PATCH = "wall"
SAC_PATCH = "wall_aneurysm"


@dataclass
class ZoneStats:
    """Area-weighted hemodynamics over one patch (or sub-region)."""

    id: str
    label: str
    patch: str
    tawss: float          # Pa
    osi: float            # 0..0.5
    rrt: float            # Pa^-1
    ecap: float           # Pa^-1
    area_mm2: float
    is_aneurysm: bool


def _area_weighted_mean(values: np.ndarray, areas: np.ndarray) -> float:
    total = float(areas.sum())
    if total <= 0:
        return 0.0
    return float((values * areas).sum() / total)


def _cell_areas_mm2(surf: pv.PolyData) -> np.ndarray:
    sized = surf.compute_cell_sizes(length=False, area=True, volume=False)
    return np.asarray(sized["Area"]) * 1e6  # m^2 -> mm^2


def _to_cell_data(surf: pv.PolyData, name: str) -> np.ndarray:
    """Fetch a field as CELL data, converting from point data if needed."""
    if name in surf.cell_data:
        return np.asarray(surf.cell_data[name])
    if name in surf.point_data:
        return np.asarray(surf.point_to_cell_data().cell_data[name])
    raise KeyError(
        f"field '{name}' not found on patch; available cell={list(surf.cell_data.keys())} "
        f"point={list(surf.point_data.keys())}"
    )


def compute_patch_fields(surf: pv.PolyData) -> dict[str, np.ndarray]:
    """
    Per-face TAWSS (Pa), OSI, RRT, ECAP and area (mm^2) for one wall patch.

    Requires the cycle-averaged fields written by controlDict:
      wallShearStressMean     -- vector, kinematic
      magWallShearStressMean  -- scalar, kinematic
    """
    # Prefer the cycle-averaged fields. Fall back to instantaneous ones so a
    # STEADY simpleFoam run can be analysed with the identical code path --
    # useful for validating units and the extraction chain quickly before
    # committing to a multi-hour transient. In the steady case OSI is 0 by
    # construction (no oscillation), which is physically correct, not a bug.
    if "wallShearStressMean" in surf.cell_data or "wallShearStressMean" in surf.point_data:
        tau_vec_mean = _to_cell_data(surf, "wallShearStressMean")
        mag_mean = _to_cell_data(surf, "magWallShearStressMean")
    else:
        tau_vec_mean = _to_cell_data(surf, "wallShearStress")
        mag_mean = np.linalg.norm(tau_vec_mean, axis=1)

    # TAWSS = mean(|tau|) * rho   <- trap 2 (mean of magnitude, not magnitude of mean)
    tawss = np.abs(mag_mean).astype(float) * RHO          # <- trap 1 (x rho)

    # OSI = 0.5 * (1 - |mean(tau_vec)| / mean(|tau|)). rho cancels, so it is
    # computed on the raw kinematic fields.
    mag_of_mean = np.linalg.norm(tau_vec_mean, axis=1)
    denom = np.where(np.abs(mag_mean) > 1e-30, np.abs(mag_mean), np.nan)
    osi = 0.5 * (1.0 - mag_of_mean / denom)
    osi = np.nan_to_num(osi, nan=0.0)
    # Numerically |mean| can marginally exceed mean|.|; clamp to the valid range.
    osi = np.clip(osi, 0.0, 0.5)

    # RRT and ECAP — identical formulas and guards to app.js:137-146 and risk.ts.
    rrt = 1.0 / np.maximum(DIVISION_FLOOR, (1.0 - 2.0 * osi) * tawss)
    ecap = osi / np.maximum(DIVISION_FLOOR, tawss)

    return {
        "tawss": tawss,
        "osi": osi,
        "rrt": rrt,
        "ecap": ecap,
        "area_mm2": _cell_areas_mm2(surf),
    }


def summarise_patch(
    surf: pv.PolyData, zone_id: str, label: str, patch: str, is_aneurysm: bool
) -> ZoneStats:
    f = compute_patch_fields(surf)
    a = f["area_mm2"]
    return ZoneStats(
        id=zone_id,
        label=label,
        patch=patch,
        tawss=_area_weighted_mean(f["tawss"], a),
        osi=_area_weighted_mean(f["osi"], a),
        rrt=_area_weighted_mean(f["rrt"], a),
        ecap=_area_weighted_mean(f["ecap"], a),
        area_mm2=float(a.sum()),
        is_aneurysm=is_aneurysm,
    )


def read_wall_patches(case_dir: Path, time_value: float | None = None) -> dict[str, pv.PolyData]:
    """Read the named wall patches from a reconstructed OpenFOAM case."""
    case_dir = Path(case_dir)
    foam = case_dir / "case.foam"
    foam.touch(exist_ok=True)

    reader = pv.OpenFOAMReader(str(foam))
    reader.enable_all_patch_arrays()
    times = list(reader.time_values)
    if not times:
        raise RuntimeError(f"no time directories found in {case_dir}")
    reader.set_active_time_value(time_value if time_value is not None else times[-1])

    mesh = reader.read()
    out: dict[str, pv.PolyData] = {}

    def walk(block: Any) -> None:
        if block is None:
            return
        if isinstance(block, pv.MultiBlock):
            for i in range(block.n_blocks):
                name = block.get_block_name(i) or ""
                child = block[i]
                if isinstance(child, pv.MultiBlock):
                    walk(child)
                elif name in (WALL_PATCH, SAC_PATCH) and child is not None:
                    out[name] = child.extract_surface()

    walk(mesh)
    missing = {WALL_PATCH, SAC_PATCH} - set(out)
    if missing:
        raise RuntimeError(f"wall patches not found in case output: {sorted(missing)}")
    return out


def analyse(case_dir: Path, time_value: float | None = None) -> dict[str, Any]:
    """Full hemodynamic summary for one solved case."""
    patches = read_wall_patches(case_dir, time_value)
    parent_surf, sac_surf = patches[WALL_PATCH], patches[SAC_PATCH]

    parent = summarise_patch(parent_surf, "inlet", "Parent Artery", WALL_PATCH, False)
    sac = summarise_patch(sac_surf, "dome", "Aneurysm Dome", SAC_PATCH, True)

    sac_f = compute_patch_fields(sac_surf)
    sac_area = sac_f["area_mm2"]
    total_sac_area = float(sac_area.sum())

    # LSAR — two definitions, deliberately both reported.
    #   relative: literature (Xiang et al. 2011) — sac area below 10% of the
    #             parent-artery mean TAWSS.
    #   absolute: the SAD's reading — sac area below 0.4 Pa.
    # They diverge whenever the parent-artery shear is far from 4 Pa, so
    # collapsing them into one number would make the metric unreproducible.
    rel_threshold = 0.10 * parent.tawss
    lsar_relative = (
        float(sac_area[sac_f["tawss"] < rel_threshold].sum()) / total_sac_area
        if total_sac_area > 0 else 0.0
    )
    lsar_absolute = (
        float(sac_area[sac_f["tawss"] < TAWSS_LOW_PA].sum()) / total_sac_area
        if total_sac_area > 0 else 0.0
    )

    # NWSS: sac shear normalised by the parent artery — dimensionless, so it is
    # comparable across patients in a way raw TAWSS is not.
    nwss = sac.tawss / parent.tawss if parent.tawss > 0 else 0.0

    return {
        "zones": [asdict(parent), asdict(sac)],
        "lsar_relative": lsar_relative,
        "lsar_absolute": lsar_absolute,
        "lsar_relative_threshold_pa": rel_threshold,
        "nwss": nwss,
        "wss_min_pa": float(np.min(sac_f["tawss"])),
        "wss_max_pa": float(np.max(np.concatenate([sac_f["tawss"], compute_patch_fields(parent_surf)["tawss"]]))),
        "rho_kg_m3": RHO,
        "flags": {
            "sac_low_tawss": sac.tawss < TAWSS_LOW_PA,
            "sac_high_osi": sac.osi > OSI_HIGH,
            "sac_high_rrt": sac.rrt > RRT_HIGH,
            "sac_high_ecap": sac.ecap > ECAP_HIGH,
        },
    }


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Extract hemodynamics from an OpenFOAM case")
    ap.add_argument("case", help="case directory (reconstructed)")
    ap.add_argument("--time", type=float, default=None)
    args = ap.parse_args()

    print(json.dumps(analyse(Path(args.case).expanduser(), args.time), indent=2))
