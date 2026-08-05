"""
Client/server risk-score parity.

The composite risk index is computed in TWO places: TypeScript in
packages/shared/src/risk.ts for the browser, and Python in
pipeline/export_patient.py for the pipeline. If they ever disagree, the
dashboard and the PDF report will show different risk scores for the same
patient, and there is no way to tell which one is right.

These tests pin the Python side to the SAME golden values that
packages/shared/src/risk.test.ts pins the TypeScript side to — the three
original demonstration patients from the pre-migration app.js. Both suites must
pass for the two implementations to be provably equivalent.

If you change a weight or a normalisation range, BOTH suites fail. That is the
intent: the scoring formula should not be editable in one language only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import re

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

REPO = Path(__file__).resolve().parents[3]

from export_patient import compute_composite, tier_of  # noqa: E402

# (tawss, osi, max_diameter_mm, aspect_ratio) -> expected composite, tier
#
# These used to carry the curated cases' AUTHORED hemodynamics — TAWSS 0.18 with
# OSI 0.38, and so on. Two reasons they could not stay:
#
#   1. No case holds those values any more. All three now derive their
#      hemodynamics from the same surrogate an upload uses, so the fixtures were
#      pinning parity on inputs the system can no longer produce.
#   2. An OSI of 0.38 saturates the recalibrated band for all three, so the OSI
#      term would read 100% in every fixture and the test would no longer
#      exercise it at all. A golden fixture outside the operating range checks
#      the clamp, not the calculation.
#
# Regenerated from the JavaScript implementation at the geometry each case
# actually carries, so parity is asserted where the system really works.
GOLDEN = {
    "PT-2025-0041": ((0.2117, 0.00913, 8.4, 2.1), 57, "High"),
    "PT-2025-0037": ((0.2884, 0.01162, 5.2, 1.4), 37, "Moderate"),
    "PT-2025-0039": ((0.4024, 0.01508, 3.1, 0.9), 18, "Low"),
}


class TestGoldenParity:
    @pytest.mark.parametrize("pid", sorted(GOLDEN))
    def test_matches_typescript_golden(self, pid):
        (tawss, osi, diameter, ar), expected, tier = GOLDEN[pid]
        out = compute_composite(tawss, osi, diameter, ar)
        assert out["composite"] == expected, (
            f"{pid}: Python gives {out['composite']}, TypeScript golden is "
            f"{expected} — the two implementations have diverged"
        )
        assert tier_of(out["composite"]) == tier

    def test_ordering_preserved(self):
        scores = {
            pid: compute_composite(*args)["composite"]
            for pid, (args, _, _) in GOLDEN.items()
        }
        assert scores["PT-2025-0041"] > scores["PT-2025-0037"] > scores["PT-2025-0039"]


class TestSubScores:
    def test_tawss_is_inverted(self):
        """LOW wall shear is the risk factor, so the sub-score rises as TAWSS falls."""
        low = compute_composite(0.2, 0.1, 5.0, 1.0)["tawssScore"]
        high = compute_composite(1.4, 0.1, 5.0, 1.0)["tawssScore"]
        assert low > high

    def test_clamped_to_0_100(self):
        for args in ((0.0, 0.9, 50.0, 9.0), (99.0, 0.0, 0.0, 0.0)):
            out = compute_composite(*args)
            for k in ("tawssScore", "osiScore", "diameterScore", "aspectScore"):
                assert 0.0 <= out[k] <= 100.0, f"{k} out of range for {args}"

    def test_osi_clamps_above_range_ceiling(self):
        """An OSI far above the band ceiling must clamp to 100, not exceed it."""
        assert compute_composite(0.18, 0.38, 8.4, 2.1)["osiScore"] == 100.0

    def test_weights_sum_to_one(self):
        """
        A uniform 100 on every sub-score must yield exactly 100 overall.

        The endpoints had to move with the bands: this used to pass TAWSS 0.15
        and OSI 0.35, which were the old extremes. Under the recalibrated bands
        0.15 Pa is comfortably inside the TAWSS range rather than at its floor,
        so the sub-score came out at 83 and the total at 95 — which looks like a
        weighting error and is nothing of the kind. The weights are unchanged;
        only where the terms saturate moved.
        """
        out = compute_composite(0.10, 0.030, 10.0, 2.5)
        assert out["tawssScore"] == 100.0
        assert out["osiScore"] == 100.0
        assert out["diameterScore"] == 100.0
        assert out["aspectScore"] == 100.0
        assert out["composite"] == 100


class TestTierBoundaries:
    """
    Boundaries moved from 75/45 to 55/32.

    The old pair did not partition anything. Across the entire geometry space —
    2 to 30 mm dome, aspect ratio 0.5 to 3.5 — the composite could only reach
    42.5 to 75.8, so nearly the whole reachable range fell inside Moderate and
    every case on the site read Moderate whatever its geometry. Low needed a 2 mm
    "aneurysm" at aspect ratio 0.5, which is not an aneurysm.

    The cause was the TAWSS band running 0.15-1.5 Pa: 1.5 Pa is healthy PARENT
    artery shear, but the term scores the SAC, which has low shear by definition.
    Every real geometry scored 81-98% on a term carrying 35% of the weight.
    Fixing the band widened the reachable range to 14.2-67.3; these boundaries
    sit at roughly a third and three fifths of that.

    See thresholds.js, which owns the definition; this mirrors it.
    """

    @pytest.mark.parametrize("score,tier", [
        (100, "High"), (55, "High"), (54, "Moderate"),
        (32, "Moderate"), (31, "Low"), (0, "Low"),
    ])
    def test_inclusive_cutoffs(self, score, tier):
        assert tier_of(score) == tier

    def test_boundaries_match_the_browser(self):
        """
        Client and server must agree on the tier, or the dashboard and the PDF
        report disagree about the same case. thresholds.js owns the numbers.
        """
        js = (REPO / "thresholds.js").read_text(encoding="utf-8")
        moderate = int(re.search(r"CRI_MODERATE:\s*(\d+)", js).group(1))
        high = int(re.search(r"CRI_HIGH:\s*(\d+)", js).group(1))
        assert tier_of(high) == "High"
        assert tier_of(high - 1) == "Moderate"
        assert tier_of(moderate) == "Moderate"
        assert tier_of(moderate - 1) == "Low"
