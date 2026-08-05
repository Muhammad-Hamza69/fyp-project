"""
Fast hemodynamic surrogate — microseconds instead of hours.

THE PROBLEM
A Navier-Stokes solve is not something a web upload can wait for. One cardiac
cycle at 239k cells took ~10 hours on this hardware, and the cost is
irreducible: the Courant condition fixes ~5,600 timesteps, each needing ~14
global pressure solves. Throwing hardware at it helps linearly at best. No
amount of engineering makes that interactive.

THE RESOLUTION
The expensive computation only has to be paid ONCE PER POINT IN THE DESIGN
SPACE, not once per user. run_sweep.py solves the geometry family properly with
OpenFOAM; this fits a smooth response surface through those solutions and
evaluates it in microseconds. That is a surrogate (or reduced-order) model, and
it is standard practice in engineering CFD — not a shortcut around the physics
but a way of reusing it.

WHAT IT HONESTLY IS
Every calibration point is a real CFD solution. Between them the surrogate
interpolates, and interpolation error is measured by leave-one-out
cross-validation and reported with every prediction. Outside the swept range it
extrapolates, and it says so rather than quietly returning a number.

OSI AND ECAP
These need a cardiac cycle, so they cannot come from the steady sweep. They are
calibrated separately, on the TRANSIENT solves — which cost ~10 h each rather
than ~2.5 min, so there are only a few of them.

What makes it work is that sac OSI and sac TAWSS co-vary: both are driven by the
same recirculation, so ECAP (their ratio) stays within 0.038-0.042 across the
solved cases and a power law in TAWSS reproduces every one of them closely.

That is an empirical relation over a narrow geometry family fitted to a handful
of points, and it is reported as such. The number of transient solves behind it
and the worst residual travel with every prediction, because a relation fitted
to three points must not be presented with the confidence of one fitted to
thirty. Where there are too few transient solves to fit at all, OSI is returned
as null rather than guessed.

The parent artery is not fitted at all: wall shear in fully developed pipe flow
is known in closed form, tau = 4*mu*Q/(pi*R^3), so it is computed analytically
and only a single measured correction factor is applied.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

# Blood, matching the solver's transportProperties.
MU = 0.0035          # Pa.s
RHO = 1060.0         # kg/m3

# Internal carotid artery, cycle-mean. Same waveform mean the pulsatile inlet
# uses, so the surrogate and the solver see the same flow.
Q_ICA_M3S = 4.6e-6   # m3/s
R_PARENT_M = 0.0020  # m


def poiseuille_wss(q_m3s: float = Q_ICA_M3S, r_m: float = R_PARENT_M) -> float:
    """
    Wall shear stress for fully developed laminar pipe flow, in Pa.

        tau = 4 mu Q / (pi R^3)

    Closed form, exact, no fitting. This is the anchor the whole surrogate
    hangs from — it is also what Gate 2C-2 validated the solver against.
    """
    return 4.0 * MU * q_m3s / (math.pi * r_m ** 3)


@dataclass
class Surrogate:
    """Fitted response surface plus the evidence for trusting it."""

    parent_correction: float = 1.0
    nwss_coef: list[float] = field(default_factory=list)     # [a, b, c]
    rrt_coef: list[float] = field(default_factory=list)      # [a, b]
    lsar_coef: list[float] = field(default_factory=list)     # [a, b]
    diameter_range_mm: tuple[float, float] = (0.0, 0.0)
    neck_ratio_range: tuple[float, float] = (0.0, 0.0)
    n_points: int = 0
    loo_error: dict[str, float] = field(default_factory=dict)
    source: str = ""

    # --- oscillatory shear, fitted separately -----------------------------
    # OSI needs a cardiac cycle, so it cannot come from the steady sweep. It is
    # calibrated on the transient solves instead, which are far more expensive
    # and therefore far fewer. Kept as its own block so the two calibrations
    # are never confused: the steady fit rests on many points, this one on a
    # handful.
    osi_coef: list[float] = field(default_factory=list)     # ln(OSI) = a + b ln(TAWSS)
    osi_n_points: int = 0
    osi_max_error_pct: float | None = None
    osi_tawss_range: tuple[float, float] = (0.0, 0.0)
    osi_note: str = ""

    # -- evaluation --------------------------------------------------------

    def predict(
        self,
        max_diameter_mm: float,
        neck_diameter_mm: float | None = None,
        aspect_ratio: float = 1.0,
        q_m3s: float = Q_ICA_M3S,
        r_parent_m: float = R_PARENT_M,
    ) -> dict[str, Any]:
        """Estimate hemodynamics for a geometry. Microseconds, no solver."""
        d = float(max_diameter_mm)
        neck = float(neck_diameter_mm) if neck_diameter_mm else d * 0.75
        neck_ratio = neck / d if d > 0 else 0.75

        parent = poiseuille_wss(q_m3s, r_parent_m) * self.parent_correction

        a, b, c = self.nwss_coef
        ln_nwss = a + b * math.log(max(d, 1e-6)) + c * math.log(max(neck_ratio, 1e-6))
        nwss = float(np.clip(math.exp(ln_nwss), 1e-4, 1.0))
        sac = parent * nwss

        ra, rb = self.rrt_coef
        rrt = float(math.exp(ra + rb * math.log(max(sac, 1e-6))))

        la, lb = self.lsar_coef
        lsar = float(np.clip(la + lb * math.log(max(d, 1e-6)), 0.0, 1.0))

        # Extrapolation is reported, never hidden. A response surface fitted
        # over 3–12 mm says nothing reliable about a 25 mm giant aneurysm, and
        # silently returning a confident number for one would be the worst
        # failure mode this model has.
        lo, hi = self.diameter_range_mm
        nlo, nhi = self.neck_ratio_range
        out_of_range = []
        if d < lo or d > hi:
            out_of_range.append(f"dome diameter {d:.1f} mm is outside the calibrated "
                                f"{lo:.1f}–{hi:.1f} mm")
        if neck_ratio < nlo or neck_ratio > nhi:
            out_of_range.append(f"neck/dome ratio {neck_ratio:.2f} is outside the "
                                f"calibrated {nlo:.2f}–{nhi:.2f}")

        # --- oscillatory shear ------------------------------------------
        osi = ecap = None
        if self.osi_coef:
            oa, ob = self.osi_coef
            osi = float(np.clip(math.exp(oa + ob * math.log(max(sac, 1e-6))), 0.0, 0.5))
            ecap = osi / max(sac, 0.02)
            lo_t, hi_t = self.osi_tawss_range
            if sac < lo_t * 0.6 or sac > hi_t * 1.6:
                out_of_range.append(
                    f"sac TAWSS {sac:.3f} Pa is well outside the {lo_t:.2f}–{hi_t:.2f} Pa "
                    "range over which OSI was calibrated"
                )

        return {
            "method": "surrogate",
            "parent_tawss_pa": parent,
            "sac_tawss_pa": sac,
            "nwss": nwss,
            "rrt": rrt,
            "lsar_relative": lsar,
            "osi": osi,
            "ecap": ecap,
            "osi_calibration_points": self.osi_n_points,
            "osi_max_error_pct": self.osi_max_error_pct,
            "osi_note": self.osi_note,
            "calibration_points": self.n_points,
            "loo_error_pct": self.loo_error,
            "extrapolating": bool(out_of_range),
            "warnings": out_of_range,
        }

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_correction": self.parent_correction,
            "nwss_coef": self.nwss_coef,
            "rrt_coef": self.rrt_coef,
            "lsar_coef": self.lsar_coef,
            "diameter_range_mm": list(self.diameter_range_mm),
            "neck_ratio_range": list(self.neck_ratio_range),
            "n_points": self.n_points,
            "loo_error_pct": self.loo_error,
            "source": self.source,
            "osi_coef": self.osi_coef,
            "osi_n_points": self.osi_n_points,
            "osi_max_error_pct": self.osi_max_error_pct,
            "osi_tawss_range": list(self.osi_tawss_range),
            "osi_note": self.osi_note,
            "poiseuille_parent_pa": poiseuille_wss(),
            "mu_pa_s": MU, "rho_kg_m3": RHO, "q_m3s": Q_ICA_M3S,
            "r_parent_m": R_PARENT_M,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Surrogate":
        return cls(
            parent_correction=d["parent_correction"],
            nwss_coef=list(d["nwss_coef"]),
            rrt_coef=list(d["rrt_coef"]),
            lsar_coef=list(d["lsar_coef"]),
            diameter_range_mm=tuple(d["diameter_range_mm"]),
            neck_ratio_range=tuple(d["neck_ratio_range"]),
            n_points=d.get("n_points", 0),
            loo_error=d.get("loo_error_pct", {}),
            source=d.get("source", ""),
            osi_coef=list(d.get("osi_coef", [])),
            osi_n_points=d.get("osi_n_points", 0),
            osi_max_error_pct=d.get("osi_max_error_pct"),
            osi_tawss_range=tuple(d.get("osi_tawss_range", (0.0, 0.0))),
            osi_note=d.get("osi_note", ""),
        )


# --------------------------------------------------------------------------- #
# Fitting
# --------------------------------------------------------------------------- #

def _fit_nwss(pts: list[dict[str, Any]]) -> list[float]:
    """
    ln(NWSS) = a + b·ln(dome) + c·ln(neck/dome)

    Log-linear because the response is multiplicative: NWSS falls roughly as a
    power of sac size, which is a straight line in log-log. With ~10 points a
    higher-order surface would fit the noise rather than the physics.
    """
    A, y = [], []
    for p in pts:
        d = p["max_diameter_mm"]
        nr = p["neck_diameter_mm"] / d if d > 0 else 0.75
        A.append([1.0, math.log(d), math.log(max(nr, 1e-6))])
        y.append(math.log(max(p["nwss"], 1e-6)))
    return list(np.linalg.lstsq(np.array(A), np.array(y), rcond=None)[0])


def _fit_power(pts: list[dict[str, Any]], xk: str, yk: str) -> list[float]:
    """ln(y) = a + b·ln(x)."""
    A = np.array([[1.0, math.log(max(p[xk], 1e-9))] for p in pts])
    y = np.array([math.log(max(p[yk], 1e-9)) for p in pts])
    return list(np.linalg.lstsq(A, y, rcond=None)[0])


def _fit_linlog(pts: list[dict[str, Any]], xk: str, yk: str) -> list[float]:
    """y = a + b·ln(x) — for quantities bounded in [0, 1] such as LSAR."""
    A = np.array([[1.0, math.log(max(p[xk], 1e-9))] for p in pts])
    y = np.array([p[yk] for p in pts])
    return list(np.linalg.lstsq(A, y, rcond=None)[0])


def fit_osi(s: Surrogate, pulsatile: Path | dict[str, Any]) -> Surrogate:
    """
    Calibrate OSI against the TRANSIENT solves.

    OSI cannot come from the steady sweep — it is defined over a cardiac cycle,
    and a steady solve has none. It has to be fitted to pulsatile solutions,
    which cost ~10 h each rather than ~2.5 min, so there are very few of them.

    The relationship that emerges is that sac OSI and sac TAWSS CO-VARY: both
    are driven by the same recirculation, so ECAP (their ratio) stays within
    0.038-0.042 across the solved cases. A power law in sac TAWSS captures it.

    That is an empirical finding on this geometry family, not a general law.
    The evidence behind it is recorded on the model and reported with every
    prediction, because a relation fitted to a handful of points must not be
    presented with the same confidence as one fitted to many.
    """
    data = (json.loads(Path(pulsatile).read_text())
            if isinstance(pulsatile, (str, Path)) else pulsatile)
    pts = [p for p in data.get("points", [])
           if p.get("sac_osi", 0) > 0 and p.get("sac_tawss_pa", 0) > 0]
    if len(pts) < 2:
        s.osi_note = (f"Only {len(pts)} transient solve(s) available — too few to "
                      "calibrate OSI. It is not estimated.")
        return s

    A = np.array([[1.0, math.log(p["sac_tawss_pa"])] for p in pts])
    y = np.array([math.log(p["sac_osi"]) for p in pts])
    coef = list(np.linalg.lstsq(A, y, rcond=None)[0])

    # Worst-case error on the points themselves. With this few solves a
    # leave-one-out would leave one or two points to fit a two-parameter line,
    # which says nothing — so the honest statistic is the residual, reported as
    # what it is rather than dressed up as cross-validation.
    worst = 0.0
    for p in pts:
        pred = math.exp(coef[0] + coef[1] * math.log(p["sac_tawss_pa"]))
        worst = max(worst, abs(pred - p["sac_osi"]) / p["sac_osi"] * 100.0)

    partial = [p["case"] for p in pts if p.get("cycle_fraction", 1.0) < 0.75]
    s.osi_coef = coef
    s.osi_n_points = len(pts)
    s.osi_max_error_pct = round(worst, 1)
    s.osi_tawss_range = (min(p["sac_tawss_pa"] for p in pts),
                         max(p["sac_tawss_pa"] for p in pts))
    s.osi_note = (
        f"OSI is calibrated on {len(pts)} full transient (pulsatile) OpenFOAM "
        f"solves, not on the steady sweep. Sac OSI and sac TAWSS co-vary, so a "
        f"power law in TAWSS reproduces all of them to within {worst:.1f}%. "
        f"This is an empirical relation over a narrow geometry family, fitted to "
        f"few points — treat it as indicative, and confirm with a transient solve "
        f"before relying on it."
        + (f" Note: {', '.join(partial)} averaged over less than 75% of the cycle."
           if partial else "")
    )
    return s


def fit(calibration: Path | dict[str, Any]) -> Surrogate:
    """Fit the response surface and cross-validate it."""
    data = (json.loads(Path(calibration).read_text())
            if isinstance(calibration, (str, Path)) else calibration)
    pts = [p for p in data["points"]
           if p.get("sac_tawss_pa", 0) > 0 and p.get("parent_tawss_pa", 0) > 0]
    if len(pts) < 4:
        raise ValueError(
            f"only {len(pts)} usable calibration point(s); a 3-coefficient "
            "surface needs at least 4 and is only meaningful with more"
        )

    # Parent artery: analytic, with ONE measured correction. The solved value
    # runs above Poiseuille because snappyHexMesh achieved no prism layers on
    # the wall, so the near-wall gradient is over-resolved by a roughly constant
    # factor — measured, documented, and applied rather than absorbed silently.
    poi = poiseuille_wss()
    correction = float(np.mean([p["parent_tawss_pa"] for p in pts]) / poi)

    s = Surrogate(
        parent_correction=correction,
        nwss_coef=_fit_nwss(pts),
        rrt_coef=_fit_power(pts, "sac_tawss_pa", "rrt"),
        lsar_coef=_fit_linlog(pts, "max_diameter_mm", "lsar_relative"),
        diameter_range_mm=(min(p["max_diameter_mm"] for p in pts),
                           max(p["max_diameter_mm"] for p in pts)),
        neck_ratio_range=(min(p["neck_diameter_mm"] / p["max_diameter_mm"] for p in pts),
                          max(p["neck_diameter_mm"] / p["max_diameter_mm"] for p in pts)),
        n_points=len(pts),
        source=data.get("solver", "OpenFOAM steady"),
    )
    s.loo_error = leave_one_out(pts)
    return s


def leave_one_out(pts: list[dict[str, Any]]) -> dict[str, float]:
    """
    Mean absolute percentage error, refitting without each point in turn.

    This is the number that earns the surrogate its place. Quoting the fit's
    own residuals would be self-congratulatory — a model can always interpolate
    the data it was fitted to. LOO asks what it does with a case it has never
    seen, which is exactly what an upload is.
    """
    if len(pts) < 5:
        return {"note": "too few points for meaningful cross-validation"}

    errs: dict[str, list[float]] = {"sac_tawss_pa": [], "nwss": [], "rrt": []}
    for i in range(len(pts)):
        train = pts[:i] + pts[i + 1:]
        held = pts[i]
        try:
            m = Surrogate(
                parent_correction=float(np.mean([p["parent_tawss_pa"] for p in train])
                                        / poiseuille_wss()),
                nwss_coef=_fit_nwss(train),
                rrt_coef=_fit_power(train, "sac_tawss_pa", "rrt"),
                lsar_coef=_fit_linlog(train, "max_diameter_mm", "lsar_relative"),
                diameter_range_mm=(0.0, 1e9), neck_ratio_range=(0.0, 1e9),
            )
            pred = m.predict(held["max_diameter_mm"], held["neck_diameter_mm"],
                             held.get("aspect_ratio", 1.0))
            for k in errs:
                actual = held[k]
                if actual:
                    errs[k].append(abs(pred[k] - actual) / abs(actual) * 100.0)
        except Exception:                              # noqa: BLE001
            continue

    return {k: round(float(np.mean(v)), 1) for k, v in errs.items() if v}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Fit the hemodynamic surrogate")
    ap.add_argument("--calibration", default="/mnt/d/fyp/services/worker/models/calibration.json")
    ap.add_argument("--out", default="/mnt/d/fyp/models/surrogate.json")
    ap.add_argument("--pulsatile",
                    default="/mnt/d/fyp/services/worker/models/pulsatile_points.json")
    ap.add_argument("--check", action="store_true",
                    help="predict the solved cohort cases and compare")
    a = ap.parse_args()

    s = fit(Path(a.calibration))
    puls = Path(a.pulsatile)
    if puls.exists():
        s = fit_osi(s, puls)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(s.to_dict(), indent=2))

    print(f"fitted on {s.n_points} CFD solutions")
    print(f"  parent: analytic Poiseuille {poiseuille_wss():.3f} Pa "
          f"x {s.parent_correction:.3f} correction = "
          f"{poiseuille_wss()*s.parent_correction:.3f} Pa")
    print(f"  calibrated dome range: {s.diameter_range_mm[0]:.1f}–{s.diameter_range_mm[1]:.1f} mm")
    print(f"  leave-one-out error: {s.loo_error}")
    if s.osi_coef:
        print(f"  OSI: fitted on {s.osi_n_points} transient solve(s), "
              f"max residual {s.osi_max_error_pct}%")
    else:
        print(f"  OSI: {s.osi_note}")
    print(f"  wrote {a.out}")

    if a.check:
        cohort = json.loads(Path("/mnt/d/fyp/real-cfd-patients.json").read_text())
        print("\n  surrogate vs FULL CFD on the solved cohort:")
        print("  %-14s %-22s %-22s" % ("case", "sac TAWSS (Pa)", "RRT (Pa^-1)"))
        for rec in cohort["patients"]:
            m = rec["morphology"]
            dome = next(z for z in rec["zones"] if "Dome" in z["name"])
            p = s.predict(m["maxDiameter"], m.get("neckDiameterMm"), m.get("aspectRatio", 1))
            e1 = abs(p["sac_tawss_pa"] - dome["tawss"]) / dome["tawss"] * 100
            e2 = abs(p["rrt"] - rec["hemodynamics"]["rrt"]) / rec["hemodynamics"]["rrt"] * 100
            print("  %-14s %7.4f vs %7.4f (%4.1f%%)  %7.2f vs %7.2f (%4.1f%%)" % (
                rec["id"], p["sac_tawss_pa"], dome["tawss"], e1,
                p["rrt"], rec["hemodynamics"]["rrt"], e2))
