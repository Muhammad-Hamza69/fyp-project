"""
Tests for the per-case aneurysm sac drawn on the brain view.

Two things are worth pinning here.

First, the sac's dimensions must come from measured morphology, because that is
the entire claim the view makes. If the mapping silently degrades to a constant,
every patient renders an identical aneurysm and the picture stops carrying
information while still looking convincing.

Second, `sac_params` in render_brain.py mirrors `buildSac` in neuro3d.js. The
browser draws the interactive view and Python draws the WebGL fallback, so if
they drift the fallback stops showing what it is supposed to be a fallback FOR.
The shared constants are asserted against the JS source directly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import render_brain  # noqa: E402

REPO = Path(__file__).resolve().parents[3]

# A stand-in for models/brain.json so these tests do not need the built asset.
META = {
    "units_per_mm": 1.8 / 167.0,
    "default_site": "MCA",
    "site_aliases": {"ACOM_PCOM_POST": "ACOM"},
    "sites": {
        "MCA": {"centre": [-0.328, 0.095, -0.271], "outward": [-0.6, 0.1, -0.79]},
        "ICA": {"centre": [-0.110, 0.156, -0.390], "outward": [-0.3, 0.2, -0.93]},
        "ACOM": {"centre": [-0.001, 0.420, -0.281], "outward": [0.0, 0.83, -0.55]},
    },
}


def _patient(pid="P1", site="MCA", max_dia=8.0, neck=6.9, ar=0.88,
             tawss=0.235, osi=0.0):
    return {
        "id": pid,
        "demographics": {"site": site},
        "morphology": {"maxDiameter": max_dia, "neckDiameterMm": neck, "aspectRatio": ar},
        "zones": [
            {"name": "Parent Artery Inlet", "tawss": 2.96, "osi": 0.0, "isAneurysm": False},
            {"name": "Aneurysm Dome", "tawss": tawss, "osi": osi, "isAneurysm": True},
        ],
    }


# --- dimensions come from the data ----------------------------------------

def test_sac_dimensions_follow_morphology():
    p = render_brain.sac_params(META, _patient(max_dia=8.0, neck=6.9, ar=0.88), "TAWSS")
    assert p["width_mm"] == pytest.approx(8.0)
    assert p["neck_mm"] == pytest.approx(6.9)
    # Aspect ratio is defined as dome height over neck width.
    assert p["height_mm"] == pytest.approx(0.88 * 6.9)
    assert p["rx"] == pytest.approx(4.0 * META["units_per_mm"])


def test_bigger_aneurysm_renders_bigger():
    """
    The monotonicity the view exists to show. The cohort spans 5.38 -> 11.02 mm
    and that ordering must survive into the rendered radius.
    """
    radii = [
        render_brain.sac_params(META, _patient(max_dia=d, neck=d * 0.75), "TAWSS")["rx"]
        for d in (5.38, 8.00, 11.02)
    ]
    assert radii == sorted(radii)
    assert radii[2] / radii[0] == pytest.approx(11.02 / 5.38, rel=1e-6)


def test_aspect_ratio_controls_height_not_width():
    low = render_brain.sac_params(META, _patient(ar=0.75), "TAWSS")
    high = render_brain.sac_params(META, _patient(ar=2.10), "TAWSS")
    assert high["ry"] > low["ry"]
    assert high["rx"] == pytest.approx(low["rx"])


def test_true_scale_not_exaggerated():
    """
    An 8 mm sac must measure 8 mm against the brain's own 167 mm. Any
    'visibility' multiplier would make the only quantitative thing in this view
    a fabrication.
    """
    p = render_brain.sac_params(META, _patient(max_dia=8.0), "TAWSS")
    assert p["rx"] * 2 / (1.8 / 167.0) == pytest.approx(8.0)


def test_missing_neck_falls_back_without_crashing():
    """Legacy demo patients record no neck diameter."""
    pat = _patient()
    del pat["morphology"]["neckDiameterMm"]
    p = render_brain.sac_params(META, pat, "TAWSS")
    assert p["neck_mm"] == pytest.approx(8.0 * 0.7)


# --- site resolution -------------------------------------------------------

@pytest.mark.parametrize("site,expected", [
    ("MCA", "MCA"),
    ("ICA", "ICA"),
    ("ica", "ICA"),                       # case-insensitive
    ("ACOM_PCOM_POST", "ACOM"),           # the grouped PHASES label
    ("SOMETHING_ELSE", "MCA"),            # unknown -> default
    ("", "MCA"),
])
def test_site_resolution(site, expected):
    p = render_brain.sac_params(META, _patient(site=site), "TAWSS")
    assert p["site"] == expected


def test_different_sites_place_the_sac_differently():
    a = render_brain.sac_params(META, _patient(site="MCA"), "TAWSS")["centre"]
    b = render_brain.sac_params(META, _patient(site="ACOM"), "TAWSS")["centre"]
    assert a != b


def test_sac_sits_outboard_of_the_vessel():
    """
    The sac is pushed along `outward` so it protrudes into open space instead of
    burrowing into the middle of the network.
    """
    p = render_brain.sac_params(META, _patient(site="MCA"), "TAWSS")
    import numpy as np

    off = np.asarray(p["centre"]) - np.asarray(p["site_centre"])
    assert float(np.dot(off, np.asarray(p["outward"]))) > 0


# --- colour ----------------------------------------------------------------

def test_low_shear_is_red_and_healthy_shear_is_blue():
    hot = render_brain.sac_params(META, _patient(tawss=0.14), "TAWSS")
    cool = render_brain.sac_params(META, _patient(tawss=2.90), "TAWSS")
    assert hot["risk_factor"] > 0.95
    assert cool["risk_factor"] == pytest.approx(0.0)
    assert hot["rgb"][0] > cool["rgb"][0]      # more red
    assert cool["rgb"][2] > hot["rgb"][2]      # more blue


def test_osi_mode_reads_osi_not_tawss():
    # Values from the calibrated band (0.002-0.030), not the old 0.03-0.35 one.
    # Under the old band both of these would have been below the floor and both
    # would have returned 0.0 — the test passed by comparing two clamped
    # results, which is how a dead normalisation survives a mirror test.
    a = render_brain.sac_params(META, _patient(tawss=0.14, osi=0.002), "OSI")
    b = render_brain.sac_params(META, _patient(tawss=0.14, osi=0.030), "OSI")
    assert a["risk_factor"] == pytest.approx(0.0)
    assert b["risk_factor"] > 0.95


def test_osi_band_covers_the_values_the_solver_produces():
    """
    The band must be REACHABLE. This is the check that was missing.

    OSI_MIN/MAX ran 0.03 to 0.35, taken from the three curated cases' authored
    OSI values. Area-weighted sac OSI from every transient solve in this project
    is 0.0096 to 0.0130 — an order of magnitude below the floor — so every real
    case clamped to 0 and rendered identically, in the browser, in the fallback
    image, and in the Composite Risk Index, which lost a third of its weight.

    A threshold no input can cross is not a strict test. It is a dead one, and
    it fails silently and forever.
    """
    solved = [0.0096, 0.0124, 0.0130]        # synthetic01, PT-2026-0101, PT-2026-0103
    for osi in solved:
        f = render_brain.sac_params(META, _patient(tawss=0.2, osi=osi), "OSI")["risk_factor"]
        assert 0.0 < f < 1.0, (
            f"sac OSI {osi} normalises to {f} — clamped, so the solver's own "
            f"output lands outside the band it is scored against"
        )

    # And distinguishable from one another, not merely non-zero.
    factors = [render_brain.sac_params(META, _patient(tawss=0.2, osi=o), "OSI")["risk_factor"]
               for o in solved]
    assert max(factors) - min(factors) > 0.05, factors


# --- the Python/JS mirror --------------------------------------------------

def test_shared_constants_match_the_browser():
    """
    render_brain.py draws the WebGL fallback for the view neuro3d.js draws
    interactively. If these constants drift the two show different pictures for
    the same case, and the fallback quietly stops being one.
    """
    js = (REPO / "neuro3d.js").read_text(encoding="utf-8")

    def num(pattern: str) -> float:
        m = re.search(pattern, js)
        assert m, f"not found in neuro3d.js: {pattern}"
        return float(m.group(1))

    assert num(r"TAWSS_MIN\s*=\s*([\d.]+)") == render_brain.TAWSS_MIN
    assert num(r"TAWSS_MAX\s*=\s*([\d.]+)") == render_brain.TAWSS_MAX
    # The OSI band moved to thresholds.js, which neuro3d.js now reads instead of
    # declaring its own copy — that duplication is how the dashboard came to
    # alert above OSI 0.3 while the worker alerted above 0.2. Assert the
    # indirection is real, then compare against the file that owns the numbers.
    assert "NeuroThresholds.OSI_RISK_LOW" in js, (
        "neuro3d.js redeclared the OSI band instead of reading thresholds.js"
    )
    thresholds = (REPO / "thresholds.js").read_text(encoding="utf-8")

    def shared(pattern: str) -> float:
        m = re.search(pattern, thresholds)
        assert m, f"not found in thresholds.js: {pattern}"
        return float(m.group(1))

    assert shared(r"OSI_RISK_LOW:\s*([\d.]+)") == render_brain.OSI_MIN
    assert shared(r"OSI_RISK_HIGH:\s*([\d.]+)") == render_brain.OSI_MAX
    assert num(r"VIEW_DISTANCE\s*=\s*([\d.]+)") == render_brain.VIEW_DISTANCE

    for name, rgb in (("STABLE_COLOR", render_brain.STABLE_RGB),
                      ("CRITICAL_COLOR", render_brain.CRITICAL_RGB)):
        m = re.search(name + r"\s*=\s*'#([0-9A-Fa-f]{6})'", js)
        assert m, f"{name} not found in neuro3d.js"
        assert [int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)] == [int(v) for v in rgb]


def test_sac_seating_factor_matches_the_browser():
    """Both must seat the sac at 0.7 x half-height along `outward`."""
    js = (REPO / "neuro3d.js").read_text(encoding="utf-8")
    assert re.search(r"addScaledVector\(OUT,\s*ry\s*\*\s*0\.7\)", js), \
        "neuro3d.js no longer seats the sac at ry * 0.7"

    import numpy as np

    p = render_brain.sac_params(META, _patient(site="MCA"), "TAWSS")
    off = np.asarray(p["centre"]) - np.asarray(p["site_centre"])
    assert float(np.linalg.norm(off)) == pytest.approx(p["ry"] * 0.7, rel=1e-9)
