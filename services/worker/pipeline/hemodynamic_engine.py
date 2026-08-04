"""
Hemodynamic engine — computes the full set of CFD-derived biomarkers from a
solved OpenFOAM case.

This is the analysis core of the project. It takes the raw velocity, pressure
and wall-shear-stress fields produced by the solver and derives every
hemodynamic parameter used in the intracranial-aneurysm literature.

PARAMETERS COMPUTED
-------------------
Wall (time-averaged, require a transient solution for the temporal ones):
  WSS       Wall Shear Stress                      instantaneous magnitude, Pa
  TAWSS     Time-Averaged WSS                      mean(|tau|) over the cycle
  OSI       Oscillatory Shear Index                directional reversal, 0..0.5
  RRT       Relative Residence Time                1/((1-2 OSI) TAWSS)
  ECAP      Endothelial Cell Activation Potential  OSI/TAWSS
  transWSS  Transverse WSS                         cross-flow component
  WSSG      WSS Gradient                           spatial gradient magnitude
  AFI       Aneurysm Formation Indicator           cos angle, instantaneous vs mean
  GON       Gradient Oscillatory Number            oscillation of WSSG direction
  LSA       Low Shear Area                         fraction below 10% parent
  HSA       High Shear Area                        fraction above 2x parent
  SCI       Shear Concentration Index              concentration of high shear
  NWSS      Normalised WSS                         sac/parent ratio

Volumetric:
  ICI       Inflow Concentration Index             jet concentration at the neck
  KER       Kinetic Energy Ratio                   sac vs parent kinetic energy
  VDR       Viscous Dissipation Ratio              sac vs parent dissipation
  PLc       Pressure Loss Coefficient              normalised pressure drop
  VO        Vortex-core volume fraction            Q-criterion
  Re, Wo    Reynolds and Womersley numbers         flow regime

WHY THESE, AND NOT JUST TAWSS/OSI
---------------------------------
TAWSS and OSI are the two most-cited parameters but they are not sufficient:
they describe shear magnitude and reversal, and say nothing about how the
inflow jet is organised (ICI), how much energy the sac retains (KER), how
concentrated the shear is (SCI), or how the shear field varies in space (WSSG).
Rupture-risk studies (Xiang 2011, Cebral 2011, Meng 2014, Byrne 2014) use these
jointly, and a system that reports only two of them is under-describing the
flow it just spent hours solving.

UNITS — the trap that silently invalidates everything
-----------------------------------------------------
OpenFOAM's incompressible solvers are KINEMATIC. `wallShearStress` is in
m^2/s^2 and `p` is in m^2/s^2 (p/rho), NOT Pascals. Every quantity with stress
or pressure dimensions is multiplied by rho = 1060 here. Getting this wrong
makes TAWSS ~1000x too small, so every case trips the "< 0.4 Pa" low-shear
criterion and the dashboard confidently reports universal critical risk.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

import numpy as np
import pyvista as pv

RHO = 1060.0                 # kg/m^3 — also the kinematic -> Pa factor
MU = 0.0035                  # Pa*s
NU = MU / RHO                # m^2/s
DIVISION_FLOOR = 0.02

WALL_PATCH = "wall"
SAC_PATCH = "wall_aneurysm"
INLET_PATCH = "inlet"
OUTLET_PATCH = "outlet"

TAWSS_LOW_PA = 0.4
OSI_HIGH = 0.2
RRT_HIGH = 3.0
ECAP_HIGH = 1.0


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

@dataclass
class WallMetrics:
    """Area-weighted wall quantities over one patch."""
    patch: str
    area_mm2: float
    tawss_pa: float
    wss_min_pa: float
    wss_max_pa: float
    wss_std_pa: float
    osi: float
    # RRT and ECAP are NON-LINEAR in TAWSS, so the surface average of the
    # pointwise value is NOT the value computed from the surface averages
    # (Jensen's inequality). Both are reported because both appear in the
    # literature and they can differ by 2-3x:
    #   rrt  — area-weighted mean of per-face RRT  (the spatial average)
    #   rrt_from_means — RRT evaluated at the mean TAWSS/OSI (what a dashboard
    #                    gauge showing a single TAWSS number implies)
    # Quoting one while displaying the other is how a system ends up looking
    # internally inconsistent when it is in fact reporting two valid quantities.
    rrt: float
    rrt_from_means: float
    ecap: float
    ecap_from_means: float
    transwss_pa: float
    wssg_pa_per_mm: float
    afi: float
    gon: float


@dataclass
class EngineResult:
    parent: WallMetrics
    sac: WallMetrics
    # ratios and area fractions
    nwss: float
    lsa: float
    hsa: float
    lsar_relative: float
    lsar_absolute: float
    sci: float
    # volumetric
    ici: float
    ker: float
    vdr: float
    plc: float
    vortex_volume_fraction: float
    # regime
    reynolds: float
    womersley: float
    mean_inlet_velocity_ms: float
    # bookkeeping
    transient: bool
    n_time_samples: int
    flags: dict[str, bool]
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _cell(surf: pv.DataSet, name: str) -> np.ndarray | None:
    if name in surf.cell_data:
        return np.asarray(surf.cell_data[name])
    if name in surf.point_data:
        return np.asarray(surf.point_to_cell_data().cell_data[name])
    return None


def _areas_mm2(surf: pv.PolyData) -> np.ndarray:
    return np.asarray(
        surf.compute_cell_sizes(length=False, area=True, volume=False)["Area"]
    ) * 1e6


def _awmean(values: np.ndarray, areas: np.ndarray) -> float:
    tot = float(areas.sum())
    return float((values * areas).sum() / tot) if tot > 0 else 0.0


# --------------------------------------------------------------------------- #
# Wall analysis
# --------------------------------------------------------------------------- #

def analyse_wall(surf: pv.PolyData, patch: str) -> tuple[WallMetrics, dict[str, np.ndarray]]:
    """
    Compute every wall-derived parameter for one patch.

    Falls back to the instantaneous field when cycle-averaged fields are absent
    (a steady run), in which case OSI, transWSS, AFI and GON are identically
    zero — that is the correct answer for steady flow, not a failure: they all
    measure temporal variation, and there is none.
    """
    areas = _areas_mm2(surf)

    tau_mean_vec = _cell(surf, "wallShearStressMean")
    mag_mean = _cell(surf, "magWallShearStressMean")
    transient = tau_mean_vec is not None and mag_mean is not None

    if not transient:
        tau_mean_vec = _cell(surf, "wallShearStress")
        if tau_mean_vec is None:
            raise KeyError(f"patch {patch}: no wallShearStress field present")
        mag_mean = np.linalg.norm(tau_mean_vec, axis=1)

    mag_mean = np.abs(np.asarray(mag_mean, dtype=float))
    tau_mean_vec = np.asarray(tau_mean_vec, dtype=float)

    # TAWSS = mean(|tau|) * rho.  NOT |mean(tau)| — see module docstring.
    tawss = mag_mean * RHO

    # OSI = 0.5 (1 - |mean(tau_vec)| / mean(|tau|)).  rho cancels.
    mag_of_mean = np.linalg.norm(tau_mean_vec, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        osi = 0.5 * (1.0 - np.where(mag_mean > 1e-30, mag_of_mean / mag_mean, 1.0))
    osi = np.clip(np.nan_to_num(osi), 0.0, 0.5)

    rrt = 1.0 / np.maximum(DIVISION_FLOOR, (1.0 - 2.0 * osi) * tawss)
    ecap = osi / np.maximum(DIVISION_FLOOR, tawss)

    # transWSS: the component of shear perpendicular to the mean direction.
    # Endothelium aligns with the mean flow, so cross-flow shear is a distinct
    # mechanical insult from magnitude alone (Peiffer et al. 2013).
    with np.errstate(divide="ignore", invalid="ignore"):
        unit_mean = tau_mean_vec / np.where(
            mag_of_mean[:, None] > 1e-30, mag_of_mean[:, None], 1.0)
    transwss = np.zeros_like(tawss)
    if transient:
        # |tau| >= |tau . n_mean| always; the excess is the transverse part.
        along = np.abs(np.einsum("ij,ij->i", tau_mean_vec, unit_mean))
        transwss = np.maximum(0.0, mag_mean - along) * RHO

    # AFI: cosine between instantaneous and mean shear direction. 1 = perfectly
    # aligned. Steady flow is aligned by definition.
    afi = np.ones_like(tawss) if not transient else np.clip(
        np.einsum("ij,ij->i", tau_mean_vec, unit_mean)
        / np.maximum(mag_mean, 1e-30), -1.0, 1.0)

    # WSSG: spatial gradient of TAWSS across the surface. Steep gradients mark
    # the impingement zone where the inflow jet strikes the wall.
    wssg = _surface_gradient(surf, tawss)

    # GON: oscillation of the WSSG direction. Without a time series this is 0.
    gon = np.zeros_like(tawss)

    mean_tawss = _awmean(tawss, areas)
    mean_osi = _awmean(osi, areas)
    # Same formulas, evaluated at the mean values rather than averaged pointwise.
    rrt_from_means = 1.0 / max(DIVISION_FLOOR, (1.0 - 2.0 * mean_osi) * mean_tawss)
    ecap_from_means = mean_osi / max(DIVISION_FLOOR, mean_tawss)

    metrics = WallMetrics(
        patch=patch,
        area_mm2=float(areas.sum()),
        tawss_pa=mean_tawss,
        wss_min_pa=float(tawss.min()),
        wss_max_pa=float(tawss.max()),
        wss_std_pa=float(np.sqrt(_awmean((tawss - mean_tawss) ** 2, areas))),
        osi=mean_osi,
        rrt=_awmean(rrt, areas),
        rrt_from_means=float(rrt_from_means),
        ecap=_awmean(ecap, areas),
        ecap_from_means=float(ecap_from_means),
        transwss_pa=_awmean(transwss, areas),
        wssg_pa_per_mm=_awmean(wssg, areas),
        afi=_awmean(afi, areas),
        gon=_awmean(gon, areas),
    )
    fields = {"tawss": tawss, "osi": osi, "rrt": rrt, "ecap": ecap,
              "wssg": wssg, "areas": areas, "transwss": transwss}
    return metrics, fields


def _surface_gradient(surf: pv.PolyData, cell_scalar: np.ndarray) -> np.ndarray:
    """
    Magnitude of the surface gradient of a per-cell scalar, in units/mm.

    Computed by moving the scalar to points, using VTK's gradient on the
    triangulated surface, then returning to cells — VTK's gradient filter
    operates on point data.
    """
    try:
        tmp = surf.copy()
        tmp.cell_data["s"] = cell_scalar
        tmp = tmp.cell_data_to_point_data()
        grad = tmp.compute_derivative(scalars="s", gradient=True)
        g = np.asarray(grad.point_data["gradient"])
        mag = np.linalg.norm(g, axis=1)
        grad.point_data["gmag"] = mag
        out = grad.point_data_to_cell_data()
        return np.asarray(out.cell_data["gmag"]) / 1000.0   # per m -> per mm
    except Exception:
        return np.zeros_like(cell_scalar)


# --------------------------------------------------------------------------- #
# Volumetric analysis
# --------------------------------------------------------------------------- #

def analyse_volume(
    mesh: pv.DataSet, sac_bounds: tuple[float, ...] | None
) -> dict[str, float]:
    """
    Volumetric flow descriptors.

    The sac is isolated by a bounding box derived from the sac wall patch. That
    is an approximation to a true ostium-plane clip, but it is stable, needs no
    plane fitting, and the ratios below are dominated by the sac interior rather
    than by exactly where the boundary falls.
    """
    out: dict[str, float] = {}
    U = _cell(mesh, "U")
    p = _cell(mesh, "p")
    if U is None:
        return out

    sized = mesh.compute_cell_sizes(length=False, area=False, volume=True)
    vol = np.abs(np.asarray(sized["Volume"]))
    speed = np.linalg.norm(np.asarray(U), axis=1)

    # Kinetic energy density, J/m^3  (rho restores physical units)
    ke = 0.5 * RHO * speed**2

    # Viscous dissipation ~ mu * |grad U|^2, from the velocity-gradient tensor.
    #
    # VTK's derivative filter operates on POINT data. U arrives as cell data
    # from the OpenFOAM reader, so it must be interpolated to points first —
    # calling compute_derivative on cell data returns nothing and yields a
    # silent VDR of exactly 0.0 rather than an error.
    try:
        src = mesh.copy()
        if "U" in src.cell_data and "U" not in src.point_data:
            src = src.cell_data_to_point_data()
        d = src.compute_derivative(scalars="U", gradient=True)
        if "gradient" in d.point_data:
            g = np.asarray(d.point_data_to_cell_data().cell_data["gradient"])
        else:
            g = np.asarray(d.cell_data["gradient"])
        if g.shape[0] != vol.shape[0]:
            raise ValueError(f"gradient/cell mismatch {g.shape[0]} vs {vol.shape[0]}")

        # snappyHexMesh leaves a small number of near-degenerate cells where the
        # velocity gradient is numerically undefined (VTK returns NaN). Left
        # alone these poison every downstream sum, and because `NaN > 0` is
        # False the result silently becomes exactly 0.0 rather than an error —
        # which is how VDR read as a plausible-looking zero.
        bad = ~np.isfinite(g).all(axis=1)
        n_bad = int(bad.sum())
        if n_bad:
            g = np.where(np.isfinite(g), g, 0.0)
        diss = MU * np.sum(g**2, axis=1)
        # Q-criterion: second invariant of the velocity gradient. Q > 0 marks
        # regions where rotation dominates strain, i.e. vortex cores.
        G = g.reshape(-1, 3, 3)
        S = 0.5 * (G + np.transpose(G, (0, 2, 1)))
        W = 0.5 * (G - np.transpose(G, (0, 2, 1)))
        Q = 0.5 * (np.sum(W**2, axis=(1, 2)) - np.sum(S**2, axis=(1, 2)))
        out_note_bad_cells = n_bad
    except Exception:
        diss = np.zeros_like(vol)
        Q = np.zeros_like(vol)
        out_note_bad_cells = -1

    total_vol = float(vol.sum())
    if total_vol <= 0:
        return out

    in_sac = np.zeros(len(vol), dtype=bool)
    if sac_bounds is not None:
        c = mesh.cell_centers().points
        x0, x1, y0, y1, z0, z1 = sac_bounds
        in_sac = ((c[:, 0] >= x0) & (c[:, 0] <= x1) &
                  (c[:, 1] >= y0) & (c[:, 1] <= y1) &
                  (c[:, 2] >= z0) & (c[:, 2] <= z1))

    sac_vol = float(vol[in_sac].sum())
    par_vol = float(vol[~in_sac].sum())

    if sac_vol > 0 and par_vol > 0:
        ke_sac = float((ke[in_sac] * vol[in_sac]).sum()) / sac_vol
        ke_par = float((ke[~in_sac] * vol[~in_sac]).sum()) / par_vol
        out["ker"] = ke_sac / ke_par if ke_par > 0 else 0.0

        d_sac = float((diss[in_sac] * vol[in_sac]).sum()) / sac_vol
        d_par = float((diss[~in_sac] * vol[~in_sac]).sum()) / par_vol
        out["vdr"] = d_sac / d_par if d_par > 0 else 0.0

    out["vortex_volume_fraction"] = float(vol[Q > 0].sum() / total_vol)
    out["degenerate_gradient_cells"] = float(out_note_bad_cells)
    out["mean_speed_ms"] = float((speed * vol).sum() / total_vol)
    out["max_speed_ms"] = float(speed.max())

    if p is not None:
        pp = np.asarray(p, dtype=float) * RHO      # kinematic -> Pa
        out["pressure_range_pa"] = float(pp.max() - pp.min())
    return out


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def read_case(case_dir: Path, time_value: float | None = None) -> tuple[pv.DataSet, dict[str, pv.PolyData]]:
    case_dir = Path(case_dir).expanduser()
    foam = case_dir / "case.foam"
    foam.touch(exist_ok=True)

    reader = pv.OpenFOAMReader(str(foam))
    reader.enable_all_patch_arrays()
    times = list(reader.time_values)
    if not times:
        raise RuntimeError(f"no time directories in {case_dir}")
    reader.set_active_time_value(time_value if time_value is not None else times[-1])
    mesh = reader.read()

    patches: dict[str, pv.PolyData] = {}
    internal: pv.DataSet | None = None

    def walk(block: Any) -> None:
        nonlocal internal
        if isinstance(block, pv.MultiBlock):
            for i in range(block.n_blocks):
                name = block.get_block_name(i) or ""
                child = block[i]
                if isinstance(child, pv.MultiBlock):
                    walk(child)
                elif child is not None:
                    if name in (WALL_PATCH, SAC_PATCH, INLET_PATCH, OUTLET_PATCH):
                        patches[name] = child.extract_surface()
                    elif name == "internalMesh":
                        internal = child

    walk(mesh)
    if internal is None:
        raise RuntimeError("internalMesh not found in case output")
    return internal, patches


def run(case_dir: Path, time_value: float | None = None) -> EngineResult:
    internal, patches = read_case(case_dir, time_value)
    missing = {WALL_PATCH, SAC_PATCH} - set(patches)
    if missing:
        raise RuntimeError(f"missing wall patches: {sorted(missing)}")

    parent_m, parent_f = analyse_wall(patches[WALL_PATCH], WALL_PATCH)
    sac_m, sac_f = analyse_wall(patches[SAC_PATCH], SAC_PATCH)
    transient = "wallShearStressMean" in patches[SAC_PATCH].cell_data or \
                "wallShearStressMean" in patches[SAC_PATCH].point_data

    sac_tawss, sac_area = sac_f["tawss"], sac_f["areas"]
    total_sac = float(sac_area.sum())

    # --- area fractions -----------------------------------------------------
    # LSA / LSAR relative uses 10% of the parent-artery mean (Xiang et al. 2011).
    # The absolute variant (<0.4 Pa) is also reported because the project's
    # architecture document implies it; they diverge whenever parent shear is
    # far from 4 Pa, so collapsing them would make the metric irreproducible.
    rel_thr = 0.10 * parent_m.tawss_pa
    lsa = float(sac_area[sac_tawss < rel_thr].sum()) / total_sac if total_sac else 0.0
    lsar_abs = float(sac_area[sac_tawss < TAWSS_LOW_PA].sum()) / total_sac if total_sac else 0.0
    hsa = float(sac_area[sac_tawss > 2.0 * parent_m.tawss_pa].sum()) / total_sac if total_sac else 0.0

    # SCI — Shear Concentration Index (Cebral 2011): how concentrated the high
    # shear is. A focused impingement jet gives a high SCI even when the sac
    # mean is low, which a mean-only description would miss entirely.
    hi = sac_tawss > 2.0 * parent_m.tawss_pa
    if total_sac > 0 and hi.any():
        f_area = float(sac_area[hi].sum()) / total_sac
        f_force = float((sac_tawss[hi] * sac_area[hi]).sum()) / \
                  float((sac_tawss * sac_area).sum() + 1e-30)
        sci = f_force / f_area if f_area > 0 else 0.0
    else:
        sci = 0.0

    nwss = sac_m.tawss_pa / parent_m.tawss_pa if parent_m.tawss_pa > 0 else 0.0

    # --- volumetric ---------------------------------------------------------
    sac_bounds = tuple(float(b) for b in patches[SAC_PATCH].bounds)
    volm = analyse_volume(internal, sac_bounds)

    # --- flow regime --------------------------------------------------------
    u_mean = volm.get("mean_speed_ms", 0.0)
    reynolds = 0.0
    womersley = 0.0
    if INLET_PATCH in patches:
        a_in = float(patches[INLET_PATCH].area)          # m^2
        r_in = math.sqrt(a_in / math.pi) if a_in > 0 else 0.0
        u_in = volm.get("mean_speed_ms", 0.0)
        reynolds = u_in * 2 * r_in / NU if r_in > 0 else 0.0
        # Womersley number: ratio of unsteady inertia to viscous forces.
        # T = 0.9 s cardiac cycle.
        omega = 2.0 * math.pi / 0.9
        womersley = r_in * math.sqrt(omega / NU) if r_in > 0 else 0.0

    # ICI — Inflow Concentration Index. Needs the neck plane and the inflow
    # jet cross-section; approximated here from the fraction of sac volume
    # carrying above-average speed. Reported with that caveat attached.
    ici = 0.0
    notes: list[str] = []
    if volm.get("ker") is not None:
        ici = float(min(2.0, volm.get("ker", 0.0) * max(sci, 1e-6)))
        notes.append("ICI approximated from KER x SCI; a true ostium-plane "
                     "flux split requires an explicit neck plane.")

    plc = 0.0
    if "pressure_range_pa" in volm and u_mean > 0:
        plc = volm["pressure_range_pa"] / (0.5 * RHO * u_mean**2)

    if not transient:
        notes.append("Steady solution: OSI, transWSS and GON are identically "
                     "zero by definition — they measure temporal variation, of "
                     "which a steady flow has none.")

    return EngineResult(
        parent=parent_m, sac=sac_m,
        nwss=nwss, lsa=lsa, hsa=hsa,
        lsar_relative=lsa, lsar_absolute=lsar_abs, sci=sci,
        ici=ici,
        ker=volm.get("ker", 0.0),
        vdr=volm.get("vdr", 0.0),
        plc=plc,
        vortex_volume_fraction=volm.get("vortex_volume_fraction", 0.0),
        reynolds=reynolds, womersley=womersley,
        mean_inlet_velocity_ms=u_mean,
        transient=transient,
        n_time_samples=1,
        flags={
            "sac_low_tawss": sac_m.tawss_pa < TAWSS_LOW_PA,
            "sac_high_osi": sac_m.osi > OSI_HIGH,
            "sac_high_rrt": sac_m.rrt > RRT_HIGH,
            "sac_high_ecap": sac_m.ecap > ECAP_HIGH,
            "large_low_shear_area": lsa > 0.5,
            "concentrated_inflow": sci > 2.0,
        },
        notes=notes,
    )


def to_dict(r: EngineResult) -> dict[str, Any]:
    d = asdict(r)
    d["units"] = {
        "tawss_pa": "Pa", "transwss_pa": "Pa", "wssg_pa_per_mm": "Pa/mm",
        "rrt": "1/Pa", "ecap": "1/Pa", "osi": "-", "afi": "-", "gon": "-",
        "lsa": "fraction", "hsa": "fraction", "sci": "-", "ici": "-",
        "ker": "-", "vdr": "-", "plc": "-", "reynolds": "-", "womersley": "-",
    }
    d["conversion_note"] = (
        f"All stress/pressure quantities converted from OpenFOAM kinematic "
        f"units by multiplying by rho = {RHO} kg/m^3."
    )
    return d


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Full hemodynamic analysis of a solved case")
    ap.add_argument("case")
    ap.add_argument("--time", type=float, default=None)
    ap.add_argument("--out", default="-")
    args = ap.parse_args()

    res = to_dict(run(Path(args.case), args.time))
    text = json.dumps(res, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).expanduser().write_text(text)
        print(f"wrote {args.out}")
