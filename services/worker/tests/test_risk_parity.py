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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from export_patient import compute_composite, tier_of  # noqa: E402

# (tawss, osi, max_diameter_mm, aspect_ratio) -> expected composite, tier
GOLDEN = {
    "PT-2025-0041": ((0.18, 0.38, 8.4, 2.1), 92, "High"),
    "PT-2025-0037": ((0.42, 0.24, 5.2, 1.4), 62, "Moderate"),
    "PT-2025-0039": ((0.85, 0.08, 3.1, 0.9), 26, "Low"),
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
        """PT-2025-0041's OSI of 0.38 exceeds the 0.35 ceiling and must clamp to 100."""
        assert compute_composite(0.18, 0.38, 8.4, 2.1)["osiScore"] == 100.0

    def test_weights_sum_to_one(self):
        """A uniform 100 on every sub-score must yield exactly 100 overall."""
        out = compute_composite(0.15, 0.35, 10.0, 2.5)
        assert out["composite"] == 100


class TestTierBoundaries:
    @pytest.mark.parametrize("score,tier", [
        (100, "High"), (75, "High"), (74, "Moderate"),
        (45, "Moderate"), (44, "Low"), (0, "Low"),
    ])
    def test_inclusive_cutoffs(self, score, tier):
        assert tier_of(score) == tier
