"""
Convergence-monitor tests.

The classifier exists to catch STAGNATION — a solve whose residuals plateau
above target without diverging. It produces a complete field, finishes without
error, and yields a risk score. These tests build synthetic solver logs with
known behaviour so each classification is checked against a case where the
right answer is not in doubt.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from convergence import ConvergenceState, analyse_log  # noqa: E402


def _log(residuals: dict[str, list[float]], *, solver: str = "simpleFoam",
         continuity: float = 1e-9, converged_at: int | None = None) -> str:
    """Render a synthetic OpenFOAM log with the given residual history."""
    n = len(next(iter(residuals.values())))
    lines = [f"Build : {solver} synthetic", ""]
    for i in range(n):
        lines.append(f"Time = {i + 1}")
        lines.append("")
        for fname, series in residuals.items():
            v = series[i]
            lines.append(
                f"smoothSolver:  Solving for {fname}, Initial residual = {v:.6e}, "
                f"Final residual = {v / 10:.6e}, No Iterations 2"
            )
        lines.append(
            f"time step continuity errors : sum local = {abs(continuity):.6e}, "
            f"global = {continuity:.6e}, cumulative = {continuity:.6e}"
        )
        lines.append("")
    if converged_at is not None:
        lines.append(f"SIMPLE solution converged in {converged_at} iterations")
    lines.append(f"{solver}")
    return "\n".join(lines)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "log.simpleFoam"
    p.write_text(text)
    return p


class TestClassification:
    def test_converged_when_solver_says_so(self, tmp_path):
        decay = list(np.logspace(-1, -8, 60))
        rep = analyse_log(_write(tmp_path, _log({"Ux": decay, "p": decay},
                                                converged_at=60)))
        assert rep.state is ConvergenceState.CONVERGED
        assert rep.trustworthy is True

    def test_converged_on_residual_target_alone(self, tmp_path):
        decay = list(np.logspace(-1, -7, 80))
        rep = analyse_log(_write(tmp_path, _log({"Ux": decay, "p": decay})))
        assert rep.state is ConvergenceState.CONVERGED

    def test_stagnation_is_detected(self, tmp_path):
        """
        THE important case: residuals fall then flatten at 1e-3, well above the
        1e-5 target. Nothing diverges, the run completes, and the fields look
        fine — but the result is not converged.
        """
        plateau = list(np.logspace(-1, -3, 30)) + [1e-3] * 50
        rep = analyse_log(_write(tmp_path, _log({"Ux": plateau, "p": plateau})))
        assert rep.state is ConvergenceState.STAGNATED
        assert rep.trustworthy is False
        assert any("RESULTS SHOULD NOT BE REPORTED" in n for n in rep.notes)

    def test_divergence_is_detected(self, tmp_path):
        rising = list(np.logspace(-4, 2, 60))
        rep = analyse_log(_write(tmp_path, _log({"Ux": rising, "p": rising})))
        assert rep.state is ConvergenceState.DIVERGED
        assert rep.trustworthy is False

    def test_incomplete_when_still_improving(self, tmp_path):
        still_falling = list(np.logspace(-1, -4, 60))   # ends above 1e-5
        rep = analyse_log(_write(tmp_path, _log({"Ux": still_falling,
                                                 "p": still_falling})))
        assert rep.state is ConvergenceState.INCOMPLETE

    def test_transient_not_judged_by_steady_criterion(self, tmp_path):
        """
        pimpleFoam residuals oscillate per timestep by design. Applying a
        steady-state convergence test to them would wrongly flag every healthy
        transient run as stagnated.
        """
        rng = np.random.default_rng(0)
        noisy = list(10 ** rng.uniform(-5, -3, 80))
        text = _log({"Ux": noisy, "p": noisy}, solver="pimpleFoam")
        p = tmp_path / "log.pimpleFoam"; p.write_text(text)
        rep = analyse_log(p, transient=True)
        assert rep.state is ConvergenceState.OSCILLATING
        assert rep.trustworthy is True


class TestMassConservation:
    def test_large_continuity_error_fails(self, tmp_path):
        decay = list(np.logspace(-1, -8, 40))
        rep = analyse_log(_write(tmp_path, _log({"Ux": decay, "p": decay},
                                                continuity=1e-3, converged_at=40)))
        assert rep.mass_conserved is False
        assert rep.trustworthy is False, (
            "converged residuals must NOT be reported as trustworthy when mass "
            "is not conserved — a bad outlet BC can drop residuals while losing mass"
        )

    def test_small_continuity_error_passes(self, tmp_path):
        decay = list(np.logspace(-1, -8, 40))
        rep = analyse_log(_write(tmp_path, _log({"Ux": decay, "p": decay},
                                                continuity=1e-10, converged_at=40)))
        assert rep.mass_conserved is True


class TestResidualStatistics:
    def test_decades_dropped(self, tmp_path):
        decay = list(np.logspace(-1, -7, 50))     # six decades
        rep = analyse_log(_write(tmp_path, _log({"p": decay})))
        f = next(x for x in rep.fields if x.field == "p")
        assert f.decades_dropped == pytest.approx(6.0, abs=0.05)

    def test_tail_slope_flat_for_plateau(self, tmp_path):
        plateau = list(np.logspace(-1, -3, 20)) + [1e-3] * 60
        rep = analyse_log(_write(tmp_path, _log({"p": plateau})))
        f = next(x for x in rep.fields if x.field == "p")
        assert abs(f.tail_slope) < 1e-6

    def test_handles_empty_log(self, tmp_path):
        rep = analyse_log(_write(tmp_path, "no residuals here\n"))
        assert rep.state is ConvergenceState.UNKNOWN
        assert rep.trustworthy is False
