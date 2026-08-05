"""
Tests for the AI rupture-prediction stage and, especially, its wiring.

The model itself was complete and correct for some time. What was broken was
that nothing consumed it: finalize.py computed the prediction AFTER writing
real-cfd-patients.json and attached it only to the PDF, so no prediction ever
reached the dashboard however good the model was. `--skip-reports` skipped the
inference entirely as a side effect.

That class of bug leaves no trace — the page renders, the tests pass, the
feature is simply absent. So these tests assert the CONNECTIONS as much as the
arithmetic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_WORKER))
sys.path.insert(0, str(_WORKER / "pipeline"))

from pipeline import risk_model  # noqa: E402

REPO = _WORKER.parent.parent
MODEL_DIR = _WORKER / "models"


def _hemo(tawss=0.235, osi=0.0, rrt=11.0, ecap=0.0, nwss=0.08, lsar=0.84):
    return {"zones": [{"id": "dome", "tawss": tawss, "osi": osi,
                       "rrt": rrt, "ecap": ecap}],
            "nwss": nwss, "lsar_relative": lsar}


MORPH = {"maxDiameter": 8.0, "aspectRatio": 0.88, "domeToNeck": 1.16,
         "nonSphericityIndex": 0.147}
DEMO = {"age": 64, "hypertension": True, "earlierSAH": False, "site": "MCA"}


# --- the model artifact ----------------------------------------------------

def test_model_artifact_ships():
    """Inference needs both files; a missing one is a silent no-prediction."""
    assert (MODEL_DIR / f"{risk_model.MODEL_VERSION}.txt").exists()
    assert (MODEL_DIR / f"{risk_model.MODEL_VERSION}.json").exists()


def test_model_declares_it_is_not_clinically_valid():
    """
    The model is trained on synthetic data. If that disclaimer ever goes
    missing, the dashboard and the PDF both render a bare probability — which
    is the most misleading thing either could show.
    """
    meta = json.loads((MODEL_DIR / f"{risk_model.MODEL_VERSION}.json").read_text())
    assert "SYNTHETIC" in meta["training_data"].upper()
    validity = meta["clinical_validity"].upper()
    assert "ILLUSTRATIVE" in validity
    assert "NOT" in validity


# --- feature extraction is deterministic ------------------------------------

def test_features_are_deterministic():
    a = risk_model.extract_features(_hemo(), MORPH, DEMO).vector()
    b = risk_model.extract_features(_hemo(), MORPH, DEMO).vector()
    assert (a == b).all()


def test_feature_vector_matches_the_trained_feature_order():
    """
    LightGBM takes a positional vector. If FEATURE_NAMES drifts from the order
    the model was trained on, every prediction silently uses the wrong columns
    and still returns a plausible probability.
    """
    meta = json.loads((MODEL_DIR / f"{risk_model.MODEL_VERSION}.json").read_text())
    assert list(meta["features"]) == list(risk_model.FEATURE_NAMES)


def test_features_accept_both_naming_conventions():
    """Morphology arrives camelCase from the export and snake_case internally."""
    camel = risk_model.extract_features(_hemo(), MORPH, DEMO)
    snake = risk_model.extract_features(
        _hemo(),
        {"max_diameter_mm": 8.0, "aspect_ratio": 0.88, "dome_to_neck": 1.16,
         "non_sphericity_index": 0.147},
        {"age": 64, "hypertension": True, "earlier_sah": False, "site": "MCA"})
    assert camel.vector().tolist() == snake.vector().tolist()


# --- prediction -------------------------------------------------------------

def test_predict_returns_a_usable_payload():
    ai = risk_model.predict(risk_model.extract_features(_hemo(), MORPH, DEMO), MODEL_DIR)
    for key in ("model_version", "probability", "risk_category", "confidence",
                "shap", "cv_auc", "clinical_validity"):
        assert key in ai, f"missing {key}"
    assert 0.0 <= ai["probability"] <= 1.0
    assert 0.0 <= ai["confidence"] <= 1.0
    assert ai["risk_category"] in ("Low", "Moderate", "High")


def test_confidence_is_distance_from_the_boundary_not_the_probability():
    """
    A 0.5 output is maximally UNCERTAIN, not "50% confident". Reporting the
    probability as confidence would invert the meaning for exactly the cases
    where it matters most — PT-2026-0102 sits at p=0.545.
    """
    ai = risk_model.predict(
        risk_model.extract_features(_hemo(tawss=0.5, rrt=5.0), MORPH, DEMO), MODEL_DIR)
    expected = min(1.0, abs(ai["probability"] - 0.5) * 2.0)
    assert ai["confidence"] == pytest.approx(expected, abs=1e-9)


def test_shap_contributions_are_signed():
    """
    Unsigned SHAP is just feature importance — a different and weaker claim.
    The sign is what lets a reader see which features pushed the prediction up
    and which pulled it down.
    """
    ai = risk_model.predict(risk_model.extract_features(_hemo(), MORPH, DEMO), MODEL_DIR)
    contribs = [s["contribution"] for s in ai["shap"]]
    assert any(c > 0 for c in contribs)
    assert any(c < 0 for c in contribs)
    # Sorted by magnitude so the card can take the top few meaningfully.
    assert contribs == sorted(contribs, key=abs, reverse=True)


def test_lower_sac_shear_does_not_lower_predicted_risk():
    """Low wall shear is a rupture risk factor; the model must not invert it."""
    healthy = risk_model.predict(
        risk_model.extract_features(_hemo(tawss=2.5, rrt=0.5), MORPH, DEMO), MODEL_DIR)
    stagnant = risk_model.predict(
        risk_model.extract_features(_hemo(tawss=0.10, rrt=14.0), MORPH, DEMO), MODEL_DIR)
    assert stagnant["probability"] >= healthy["probability"]


# --- the wiring that was actually broken ------------------------------------

def test_export_carries_a_prediction_for_every_case():
    """
    The regression test for the original defect. finalize.py wrote the JSON
    before computing the prediction, so this block was absent for every case
    and the dashboard had nothing to render.
    """
    doc = json.loads((REPO / "real-cfd-patients.json").read_text())
    for rec in doc["patients"]:
        assert "ml" in rec, f"{rec['id']} has no ml block"
        ml = rec["ml"]
        assert 0.0 <= ml["probability"] <= 1.0
        assert ml["shap"], f"{rec['id']} has no SHAP attribution"
        assert ml["model_version"]


def test_export_flags_predictions_made_on_incomplete_inputs():
    """
    OSI and ECAP are model inputs. On a steady solve they are absent, and the
    model receives zeros — a value, not a gap. Every such case must say so, or
    the probability reads as though it were computed from a full vector.
    """
    doc = json.loads((REPO / "real-cfd-patients.json").read_text())
    for rec in doc["patients"]:
        transient = rec.get("hemodynamics", {}).get("transient", True)
        assert rec["ml"]["inputs_complete"] == bool(transient), (
            f"{rec['id']}: inputs_complete disagrees with hemodynamics.transient")


def test_finalize_attaches_prediction_before_writing_json():
    """
    Guards the ordering directly. If the AI stage drifts back below the
    PATIENTS_JSON write, predictions silently stop reaching the dashboard while
    the PDF keeps working — which is exactly how this went unnoticed.
    """
    src = (_WORKER / "finalize.py").read_text(encoding="utf-8")
    ai_at = src.index('r["ml"] = ai')
    write_at = src.index("PATIENTS_JSON.write_text")
    assert ai_at < write_at, (
        "the AI stage runs after the export is written — predictions will not "
        "reach the dashboard"
    )


def test_the_validity_caveat_survives_somewhere_authoritative():
    """
    The dashboard's ML panel has been stripped, on request, to the SHAP
    attribution alone: the probability, risk category, confidence, card title
    and on-screen validity banner are all gone.

    This test used to assert the banner was on screen. It cannot any more, so it
    asserts the thing that actually matters instead — that the statement is
    still MADE somewhere authoritative rather than having quietly evaporated
    along with the element that displayed it. Two places, both of which travel
    with the result:

      - the model artifact itself carries `clinical_validity`
      - the PDF report prints it under "Model caveat"

    If a future change drops either, this fails, and the project would then be
    shipping SHAP attributions from a synthetic-cohort model with a 0.62 AUC
    with no statement anywhere of what the model is.
    """
    meta = json.loads((MODEL_DIR / f"{risk_model.MODEL_VERSION}.json").read_text())
    validity = meta.get("clinical_validity", "")
    assert "not trained or validated on patient data" in validity.lower()
    assert meta.get("training_data", "").upper().startswith("SYNTHETIC")

    report = (REPO / "services/worker/pipeline/report.py").read_text(encoding="utf-8")
    assert "Model caveat" in report, "the PDF is the last place this is stated"
    assert "clinical_validity" in report

    # And the stripped elements must stay stripped: re-adding markup without the
    # renderer, or the reverse, puts a dash or a stale value back on screen.
    app = (REPO / "app.js").read_text(encoding="utf-8")
    html = (REPO / "index.html").read_text(encoding="utf-8")
    assert "renderMlPrediction" in app
    assert "ml-shap" in app, "the attribution is what the panel is now for"
    for gone in ("ml-probability", "ml-category", "ml-confidence", "ml-validity"):
        assert gone not in html, f"{gone} was removed from the interface"
        assert gone not in app, f"{gone} was removed from the interface"
