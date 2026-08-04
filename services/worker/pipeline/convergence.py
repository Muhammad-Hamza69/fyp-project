"""
CFD convergence monitoring — SAD module 11.5.

Parses solver residuals, verifies mass conservation, and classifies the run so
a result is never reported without knowing whether the solve was actually
trustworthy.

WHY THIS IS A SEPARATE MODULE AND NOT A GREP
--------------------------------------------
"Did it converge?" has more than two answers, and the dangerous ones look like
success:

  CONVERGED   residuals fell below target and stayed there
  STAGNATED   residuals plateaued above target — the solver stopped improving
              but did not blow up. The fields look plausible and the run
              "finished". This is the state most likely to be mistaken for a
              result.
  DIVERGED    residuals grew; usually obvious, sometimes only in one field
  INCOMPLETE  hit endTime while still improving — under-converged, not wrong,
              but the reported TAWSS is not the converged value
  OSCILLATING normal for a transient run, meaningless for a steady one

A steady case that stagnates at 1e-3 will still produce a wall-shear-stress
field and a risk score. Nothing errors. The only way to catch it is to look at
the residual history, which is what this does.

Mass conservation is checked independently of residuals: continuity error is
the physics-level statement that what flows in flows out, and a solve can show
falling residuals while quietly losing mass through a bad boundary condition.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np


class ConvergenceState(str, Enum):
    CONVERGED = "CONVERGED"
    STAGNATED = "STAGNATED"
    DIVERGED = "DIVERGED"
    INCOMPLETE = "INCOMPLETE"
    OSCILLATING = "OSCILLATING"
    UNKNOWN = "UNKNOWN"


# `Solving for Ux, Initial residual = 1.23e-04, Final residual = ...`
_RES = re.compile(
    r"Solving for (\w+),\s+Initial residual = ([0-9.eE+-]+),\s+Final residual = ([0-9.eE+-]+)"
)
_TIME = re.compile(r"^Time = ([0-9.eE+-]+)", re.M)
_CONTINUITY = re.compile(
    r"time step continuity errors :\s*sum local = ([0-9.eE+-]+),\s*global = ([0-9.eE+-]+),"
    r"\s*cumulative = ([0-9.eE+-]+)"
)
_CONVERGED_MSG = re.compile(r"(SIMPLE|PIMPLE) solution converged in (\d+) iterations")
_COURANT = re.compile(r"Courant Number mean: ([0-9.eE+-]+) max: ([0-9.eE+-]+)")


@dataclass
class FieldResidual:
    field: str
    initial: float          # last recorded initial residual
    minimum: float
    n_samples: int
    # Order-of-magnitude reduction from the first sample to the last.
    decades_dropped: float
    # Slope of log10(residual) over the final quarter; ~0 means plateaued.
    tail_slope: float


@dataclass
class ConvergenceReport:
    state: ConvergenceState
    solver: str
    iterations: int
    reported_converged_at: int | None
    fields: list[FieldResidual]
    continuity_final: float | None
    continuity_cumulative: float | None
    courant_max: float | None
    mass_conserved: bool
    trustworthy: bool
    notes: list[str] = field(default_factory=list)


def _decades(series: np.ndarray) -> float:
    a, b = series[0], series[-1]
    if a <= 0 or b <= 0:
        return 0.0
    return float(np.log10(a) - np.log10(b))


def _tail_slope(series: np.ndarray, frac: float = 0.25) -> float:
    """log10-slope over the final `frac` of the history. ~0 means a plateau."""
    n = max(4, int(len(series) * frac))
    tail = series[-n:]
    tail = np.where(tail > 0, tail, np.nan)
    if np.isnan(tail).all():
        return 0.0
    y = np.log10(np.nan_to_num(tail, nan=np.nanmin(tail)))
    x = np.arange(len(y), dtype=float)
    return float(np.polyfit(x, y, 1)[0])


def analyse_log(
    log_path: Path,
    target: float = 1e-5,
    transient: bool | None = None,
) -> ConvergenceReport:
    text = Path(log_path).read_text(errors="ignore")
    notes: list[str] = []

    solver = "unknown"
    for name in ("pimpleFoam", "simpleFoam", "icoFoam", "potentialFoam"):
        if name in text:
            solver = name
            break
    if transient is None:
        transient = solver in ("pimpleFoam", "icoFoam")

    times = _TIME.findall(text)
    iterations = len(times)

    series: dict[str, list[float]] = {}
    for fname, initial, _final in _RES.findall(text):
        try:
            series.setdefault(fname, []).append(float(initial))
        except ValueError:
            continue

    fields: list[FieldResidual] = []
    for fname, vals in series.items():
        arr = np.asarray(vals, dtype=float)
        arr = arr[np.isfinite(arr)]
        if arr.size < 2:
            continue
        fields.append(FieldResidual(
            field=fname,
            initial=float(arr[-1]),
            minimum=float(arr.min()),
            n_samples=int(arr.size),
            decades_dropped=_decades(arr),
            tail_slope=_tail_slope(arr),
        ))
    fields.sort(key=lambda f: f.field)

    cont = _CONTINUITY.findall(text)
    continuity_final = float(cont[-1][1]) if cont else None       # global
    continuity_cum = float(cont[-1][2]) if cont else None         # cumulative

    cour = _COURANT.findall(text)
    courant_max = float(cour[-1][1]) if cour else None

    m = _CONVERGED_MSG.search(text)
    reported_at = int(m.group(2)) if m else None

    # --- classify -----------------------------------------------------------
    state = ConvergenceState.UNKNOWN
    if not fields:
        notes.append("no residual lines found — solver may not have started")
        state = ConvergenceState.UNKNOWN
    elif reported_at is not None:
        state = ConvergenceState.CONVERGED
        notes.append(f"solver reported convergence at iteration {reported_at}")
    else:
        worst = max(f.initial for f in fields)
        rising = [f.field for f in fields if f.tail_slope > 0.02]
        flat = [f.field for f in fields if abs(f.tail_slope) < 1e-3]

        if rising and worst > target * 100:
            state = ConvergenceState.DIVERGED
            notes.append(f"residuals rising in: {', '.join(rising)}")
        elif transient:
            # A transient run's per-timestep residuals do not monotonically
            # fall; judging it by a steady-state criterion is meaningless.
            state = ConvergenceState.OSCILLATING
            notes.append("transient solver: per-timestep residuals are expected "
                         "to oscillate; convergence is judged per timestep, not "
                         "across the run")
        elif worst <= target:
            state = ConvergenceState.CONVERGED
        elif flat:
            state = ConvergenceState.STAGNATED
            notes.append(f"residuals plateaued above target in: {', '.join(flat)} "
                         f"(worst {worst:.2e} vs target {target:.0e}) — the fields "
                         "will still look plausible, but they are not converged")
        else:
            state = ConvergenceState.INCOMPLETE
            notes.append(f"still improving at the last iteration "
                         f"(worst {worst:.2e} vs target {target:.0e}); "
                         "endTime reached before convergence")

    # --- mass conservation --------------------------------------------------
    # Continuity error is dimensionless-ish here; 1e-6 is a conventional bar.
    mass_conserved = True
    if continuity_final is None:
        notes.append("no continuity error reported — mass conservation unverified")
        mass_conserved = False
    elif abs(continuity_final) > 1e-6:
        mass_conserved = False
        notes.append(f"global continuity error {continuity_final:.2e} exceeds 1e-6 — "
                     "mass is not conserved; check the outlet boundary condition")

    if transient and courant_max is not None and courant_max > 10:
        notes.append(f"maximum Courant number {courant_max:.1f} is high; "
                     "temporal accuracy may be degraded")

    trustworthy = (
        state in (ConvergenceState.CONVERGED, ConvergenceState.OSCILLATING)
        and mass_conserved
    )
    if not trustworthy:
        notes.append("RESULTS SHOULD NOT BE REPORTED AS CONVERGED")

    return ConvergenceReport(
        state=state,
        solver=solver,
        iterations=iterations,
        reported_converged_at=reported_at,
        fields=fields,
        continuity_final=continuity_final,
        continuity_cumulative=continuity_cum,
        courant_max=courant_max,
        mass_conserved=mass_conserved,
        trustworthy=trustworthy,
        notes=notes,
    )


def residual_history(log_path: Path, field_name: str = "p") -> list[float]:
    """Full initial-residual history for one field — for plotting."""
    text = Path(log_path).read_text(errors="ignore")
    return [float(i) for f, i, _ in _RES.findall(text) if f == field_name]


def find_solver_log(case_dir: Path) -> Path | None:
    case_dir = Path(case_dir).expanduser()
    for name in ("log.pimpleFoam", "log.simpleFoam", "log.solver"):
        p = case_dir / name
        if p.exists():
            return p
    return None


def analyse_case(case_dir: Path, target: float = 1e-5) -> ConvergenceReport:
    log = find_solver_log(case_dir)
    if log is None:
        return ConvergenceReport(
            state=ConvergenceState.UNKNOWN, solver="unknown", iterations=0,
            reported_converged_at=None, fields=[], continuity_final=None,
            continuity_cumulative=None, courant_max=None, mass_conserved=False,
            trustworthy=False, notes=[f"no solver log found in {case_dir}"],
        )
    return analyse_log(log, target)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Assess CFD convergence for a case")
    ap.add_argument("case")
    ap.add_argument("--target", type=float, default=1e-5)
    args = ap.parse_args()

    rep = analyse_case(Path(args.case), args.target)
    print(json.dumps(asdict(rep), indent=2, default=str))
