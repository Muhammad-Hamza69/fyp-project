"""
Hemodynamics correctness tests.

The most important test in this file is the analytic Poiseuille check. Wall
shear stress is the number the entire clinical interpretation rests on, and the
single most likely way to get it wrong is silently: OpenFOAM's incompressible
solvers are kinematic, so `wallShearStress` is in m^2/s^2, not Pascals. Omit
the x rho conversion and every TAWSS reads ~1000x too low, every case trips the
"< 0.4 Pa" low-shear criterion, and the dashboard reports universal critical
risk with complete confidence and no error.

These tests construct fields with known answers rather than reading solver
output, so they run in milliseconds and fail loudly on a regression.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest
import pyvista as pv

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import hemodynamic_engine as hx  # noqa: E402

RHO = 1060.0
MU = 0.0035


def _patch_with_wss(kinematic_wss, areas_hint: int = 4) -> pv.PolyData:
    """
    A small triangulated patch carrying a prescribed wallShearStress vector.

    `kinematic_wss` may be a scalar or an array; it is broadcast/resized to the
    triangulated cell count rather than assumed to match it. Triangulating an
    NxN plane yields 2*N*N cells, not N*N, and hardcoding a length here made
    the tests fail on the fixture instead of on the code.
    """
    plane = pv.Plane(i_size=1.0, j_size=1.0, i_resolution=areas_hint,
                     j_resolution=areas_hint).triangulate()
    n = plane.n_cells
    arr = np.asarray(kinematic_wss, dtype=float).ravel()
    if arr.size == 1:
        arr = np.full(n, float(arr[0]))
    elif arr.size != n:
        arr = np.resize(arr, n)
    vec = np.zeros((n, 3))
    vec[:, 0] = arr
    plane.cell_data["wallShearStress"] = vec
    return plane


class TestUnitConversion:
    def test_kinematic_to_pascals(self):
        """TAWSS must be the kinematic value multiplied by rho."""
        kinematic = 0.0025          # m^2/s^2
        surf = _patch_with_wss(np.full(32, kinematic))
        metrics, _ = hx.analyse_wall(surf, "wall")
        assert metrics.tawss_pa == pytest.approx(kinematic * RHO, rel=1e-9)

    def test_missing_conversion_would_be_caught(self):
        """
        Guards the specific regression: if someone drops the x rho factor, the
        value lands three orders of magnitude low and below every clinical
        threshold. This asserts the magnitude is physiological, not just
        self-consistent.
        """
        surf = _patch_with_wss(np.full(32, 0.0028))   # ~3 Pa once converted
        metrics, _ = hx.analyse_wall(surf, "wall")
        assert 0.1 < metrics.tawss_pa < 10.0, (
            f"TAWSS {metrics.tawss_pa} Pa is outside the physiological range; "
            "the kinematic->Pa conversion is probably missing"
        )


class TestPoiseuille:
    """
    Analytic reference: fully developed laminar pipe flow has

        tau_w = 4 * mu * Q / (pi * r^3)

    This is the closed-form answer the solver output was validated against
    (computed 2.97 Pa vs analytic 2.56 Pa, +16%, attributed to the absent
    prism layers). Here we check the arithmetic itself.
    """

    @staticmethod
    def analytic_wss(q_m3s: float, radius_m: float) -> float:
        return 4.0 * MU * q_m3s / (math.pi * radius_m**3)

    def test_known_value(self):
        # 4.6 mL/s through a 2 mm radius artery
        tau = self.analytic_wss(4.6e-6, 0.002)
        assert tau == pytest.approx(2.5624, rel=1e-3)

    def test_scales_inversely_with_r_cubed(self):
        base = self.analytic_wss(4.6e-6, 0.002)
        half = self.analytic_wss(4.6e-6, 0.001)
        assert half == pytest.approx(base * 8.0, rel=1e-9)

    def test_round_trip_through_engine(self):
        """Feed the analytic answer in as kinematic WSS; expect it back in Pa."""
        tau_pa = self.analytic_wss(4.6e-6, 0.002)
        surf = _patch_with_wss(np.full(32, tau_pa / RHO))
        metrics, _ = hx.analyse_wall(surf, "wall")
        assert metrics.tawss_pa == pytest.approx(tau_pa, rel=1e-9)


class TestAreaWeighting:
    def test_large_cells_dominate(self):
        """
        Patch faces vary by an order of magnitude after refinement, so a plain
        arithmetic mean over-weights small cells. This builds two unequal
        triangles and checks the result follows AREA, not face count.
        """
        pts = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0],   # large
                        [0, 0, 1], [1, 0, 1], [0, 1, 1]], float)
        faces = np.hstack([[3, 0, 1, 2], [3, 3, 4, 5]])
        surf = pv.PolyData(pts, faces)
        vec = np.zeros((2, 3))
        vec[0, 0] = 1.0 / RHO      # large face -> 1 Pa
        vec[1, 0] = 100.0 / RHO    # tiny face  -> 100 Pa
        surf.cell_data["wallShearStress"] = vec

        metrics, _ = hx.analyse_wall(surf, "wall")
        # Arithmetic mean would be 50.5 Pa; area-weighted is dominated by the
        # 100x larger face and must stay near 1 Pa.
        assert metrics.tawss_pa < 5.0, (
            f"got {metrics.tawss_pa} Pa — looks like an unweighted mean"
        )


class TestOSI:
    def test_steady_flow_gives_zero(self):
        """OSI measures temporal reversal; steady flow has none."""
        surf = _patch_with_wss(np.full(32, 0.002))
        metrics, _ = hx.analyse_wall(surf, "wall")
        assert metrics.osi == pytest.approx(0.0, abs=1e-6)

    def test_fully_reversing_flow_approaches_half(self):
        """
        OSI = 0.5(1 - |mean(tau_vec)| / mean(|tau|)). When the mean vector
        cancels to zero but the magnitude does not, OSI reaches its 0.5 ceiling.
        """
        surf = _patch_with_wss(np.zeros(32))
        n = surf.n_cells
        surf.cell_data["wallShearStressMean"] = np.zeros((n, 3))       # cancels
        surf.cell_data["magWallShearStressMean"] = np.full(n, 0.002)   # does not
        metrics, _ = hx.analyse_wall(surf, "wall")
        assert metrics.osi == pytest.approx(0.5, abs=1e-6)

    def test_never_exceeds_physical_range(self):
        surf = _patch_with_wss(np.zeros(16))
        n = surf.n_cells
        rng = np.random.default_rng(0)
        surf.cell_data["wallShearStressMean"] = rng.normal(0, 1e-3, (n, 3))
        surf.cell_data["magWallShearStressMean"] = np.full(n, 1e-4)  # < |mean|
        _, fields = hx.analyse_wall(surf, "wall")
        assert fields["osi"].min() >= 0.0
        assert fields["osi"].max() <= 0.5


class TestGuards:
    def test_rrt_survives_osi_half(self):
        """RRT = 1/((1-2 OSI) TAWSS) is singular at OSI -> 0.5."""
        surf = _patch_with_wss(np.zeros(16))
        n = surf.n_cells
        surf.cell_data["wallShearStressMean"] = np.zeros((n, 3))
        surf.cell_data["magWallShearStressMean"] = np.full(n, 0.002)
        metrics, fields = hx.analyse_wall(surf, "wall")
        assert np.isfinite(fields["rrt"]).all()
        assert metrics.rrt == pytest.approx(1.0 / hx.DIVISION_FLOOR, rel=1e-6)

    def test_ecap_survives_zero_tawss(self):
        surf = _patch_with_wss(np.zeros(16))
        _, fields = hx.analyse_wall(surf, "wall")
        assert np.isfinite(fields["ecap"]).all()


class TestJensenGap:
    def test_rrt_definitions_differ_for_nonuniform_fields(self):
        """
        RRT is reciprocal in TAWSS, so the surface average of pointwise RRT is
        NOT RRT computed from the mean TAWSS. Both are reported precisely
        because they differ; this pins that they are actually distinct so the
        two fields cannot silently collapse into one.
        """
        surf = _patch_with_wss(np.zeros(32))
        n = surf.n_cells
        vals = np.linspace(0.05, 3.0, n) / RHO      # strongly non-uniform
        vec = np.zeros((n, 3)); vec[:, 0] = vals
        surf.cell_data["wallShearStress"] = vec
        metrics, _ = hx.analyse_wall(surf, "wall")
        assert metrics.rrt > metrics.rrt_from_means, (
            "area-weighted RRT should exceed RRT-from-means for a non-uniform "
            "field (Jensen's inequality on a convex reciprocal)"
        )
