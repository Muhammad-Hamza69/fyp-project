"""
Tests for the quantified mesh gate.

The gate can WAIVE a checkMesh failure, which makes it the one piece of the
pipeline whose bugs let bad meshes through silently. The tests that matter most
here are therefore the negative ones: that it refuses to waive when it lacks
evidence, and that it still fails when the bad faces are somewhere that counts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import mesh_gate  # noqa: E402

# Trimmed from the real log of ~/cases/pulsatile_coarse, the mesh that exposed
# the original binary gate: 3 skew faces out of ~320,000, all on the outlet cap.
LOG_MARGINAL_SKEW = """
Checking geometry...
    Overall domain bounding box (0 -0.004 -0.004) (0.1 0.0074 0.004)
    Mesh has 3 solution (non-empty) directions (1 1 1)
    Max aspect ratio = 13.804484 OK.
    Mesh non-orthogonality Max: 59.179338 average: 5.8414414
    Non-orthogonality check OK.
    Face pyramids OK.
 ***Max skewness = 4.4039412, 3 highly skew faces detected which may impair the quality of the results
  <<Writing 3 skew faces to set skewFaces
    Coupled point location match (average 0) OK.

Failed 1 mesh checks.

End
"""

LOG_CLEAN = """
Checking geometry...
    cells:            511015
    Max aspect ratio = 9.1 OK.
    Mesh non-orthogonality Max: 55.300715 average: 6.7248788
    Non-orthogonality check OK.
    Max skewness = 3.1754568 OK.

Mesh OK.

End
"""

LOG_BAD_NON_ORTHO = LOG_CLEAN.replace(
    "Mesh non-orthogonality Max: 55.300715", "Mesh non-orthogonality Max: 71.4"
).replace("Mesh OK.", "Failed 1 mesh checks.")

# Outlet cap, ~50 mm downstream of a sac at x = 50 mm.
FACES_AT_OUTLET = [(0.09991, 0.00194, -0.00042),
                   (0.09962, -0.00113, -0.00163),
                   (0.09961, -0.00142, -0.00137)]

SAC_CENTRE = (0.050, 0.0, 0.0)
SAC_RADIUS = 0.015


_seq = 0


def _case(tmp_path: Path, log: str) -> Path:
    """A throwaway case directory. Numbered so one test can build several."""
    global _seq
    _seq += 1
    case = tmp_path / f"case{_seq}"
    case.mkdir(parents=True, exist_ok=True)
    (case / "log.checkMesh").write_text(log)
    return case


def test_clean_mesh_passes(tmp_path):
    g = mesh_gate.evaluate(_case(tmp_path, LOG_CLEAN), SAC_CENTRE, SAC_RADIUS)
    assert g.passed
    assert g.failures == []
    assert g.max_non_ortho == pytest.approx(55.300715)
    assert g.max_skewness == pytest.approx(3.1754568)
    assert g.n_cells == 511015


def test_non_orthogonality_is_never_waived(tmp_path):
    """
    Non-orthogonality damages the pressure Laplacian across the whole mesh, so
    unlike skewness it cannot be excused by where the faces are. Supplying a
    region of interest must not rescue it.
    """
    g = mesh_gate.evaluate(_case(tmp_path, LOG_BAD_NON_ORTHO), SAC_CENTRE, SAC_RADIUS)
    assert not g.passed
    assert any("non-orthogonality" in f for f in g.failures)
    assert g.waivers == []


def test_skew_outside_roi_is_waived(tmp_path, monkeypatch):
    monkeypatch.setattr(mesh_gate, "_skew_face_centres_m", lambda *a, **k: FACES_AT_OUTLET)
    g = mesh_gate.evaluate(_case(tmp_path, LOG_MARGINAL_SKEW), SAC_CENTRE, SAC_RADIUS)
    assert g.passed
    assert len(g.waivers) == 1
    assert "outside the region of interest" in g.waivers[0]
    # The waiver must report how many faces it excused, so it is auditable.
    assert "3 offending faces" in g.waivers[0]


def test_skew_inside_roi_still_fails(tmp_path, monkeypatch):
    """The same violation must fail when the faces sit where results are read."""
    monkeypatch.setattr(mesh_gate, "_skew_face_centres_m", lambda *a, **k: FACES_AT_OUTLET)
    g = mesh_gate.evaluate(
        _case(tmp_path, LOG_MARGINAL_SKEW),
        roi_centre_m=(0.099, 0.0, 0.0),      # ROI moved onto the outlet
        roi_radius_m=SAC_RADIUS,
    )
    assert not g.passed
    assert any("INSIDE the region of interest" in f for f in g.failures)


def test_refuses_to_waive_without_face_locations(tmp_path, monkeypatch):
    """
    Fails closed. `None` means the faces could not be located — no faceSet, no
    foamToVTK, unreadable output. A gate that waives on missing evidence is not
    a gate.
    """
    monkeypatch.setattr(mesh_gate, "_skew_face_centres_m", lambda *a, **k: None)
    g = mesh_gate.evaluate(_case(tmp_path, LOG_MARGINAL_SKEW), SAC_CENTRE, SAC_RADIUS)
    assert not g.passed
    assert any("refusing to waive without evidence" in f for f in g.failures)


def test_refuses_to_waive_without_a_roi(tmp_path, monkeypatch):
    """With no region of interest there is no basis to call a face irrelevant."""
    monkeypatch.setattr(mesh_gate, "_skew_face_centres_m", lambda *a, **k: FACES_AT_OUTLET)
    g = mesh_gate.evaluate(_case(tmp_path, LOG_MARGINAL_SKEW))
    assert not g.passed
    assert any("no region of interest" in f for f in g.failures)


def test_missing_log_fails(tmp_path):
    case = tmp_path / "empty"
    case.mkdir()
    g = mesh_gate.evaluate(case, SAC_CENTRE, SAC_RADIUS)
    assert not g.passed
    assert any("did not run" in f for f in g.failures)


def test_truncated_log_fails(tmp_path):
    """Neither 'Mesh OK' nor a failure count means the log is unusable."""
    g = mesh_gate.evaluate(_case(tmp_path, "Checking geometry...\n"), SAC_CENTRE, SAC_RADIUS)
    assert not g.passed


def test_exit_code_matches_passed(tmp_path):
    """The shell scripts branch on the exit code, so it must track `passed`."""
    assert mesh_gate.evaluate(_case(tmp_path, LOG_CLEAN), SAC_CENTRE, SAC_RADIUS).passed is True
    assert mesh_gate.evaluate(_case(tmp_path, LOG_BAD_NON_ORTHO), SAC_CENTRE,
                              SAC_RADIUS).passed is False
