"""
AI rupture-risk module — feature extraction, prediction, and explainability.

Split into three separable stages, matching the architecture document:

  1. FEATURE_EXTRACTION  deterministic, cacheable, no model involved
  2. RISK_PREDICTION     a versioned model artefact, swappable
  3. COMPOSITE_RISK      the transparent weighted formula

They are kept apart for a specific reason: **the composite risk score must keep
working when the ML model is absent or declines to predict.** Collapsing them
into one step makes that fallback impossible, and a clinical tool that returns
nothing when a model file is missing is worse than one that returns the
transparent score.

HONESTY ABOUT THE MODEL
-----------------------
There is no labelled patient cohort available for this project (AneuriskWeb,
which carried rupture status, is offline). The model here is therefore trained
on a **synthetic cohort** generated from published relationships between
morphology, hemodynamics and rupture — principally the PHASES study (Greving et
al., Lancet Neurol 2014) and the low-WSS/high-OSI literature (Xiang et al. 2011;
Meng et al. 2014).

That makes it **illustrative, not clinically validated**. It demonstrates that
the feature pipeline, model interface and SHAP explainability work end to end.
It must not be presented as a validated predictor, and its output is reported
alongside — never instead of — the transparent composite index.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np

MODEL_VERSION = "lgbm-synth-v1"

# Feature order is part of the model contract: reordering silently corrupts
# inference because LightGBM indexes by position.
FEATURE_NAMES = [
    "tawss_sac_pa",
    "osi_sac",
    "rrt_sac",
    "ecap_sac",
    "nwss",
    "lsar_relative",
    "max_diameter_mm",
    "aspect_ratio",
    "dome_to_neck",
    "non_sphericity_index",
    "age",
    "hypertension",
    "earlier_sah",
    "site_score",
]

SITE_SCORE = {"ICA": 0.0, "MCA": 2.0, "ACOM_PCOM_POST": 4.0}


@dataclass
class Features:
    values: dict[str, float]

    def vector(self) -> np.ndarray:
        return np.array([[self.values.get(n, 0.0) for n in FEATURE_NAMES]], dtype=float)


def extract_features(
    hemo: dict[str, Any], morphology: dict[str, Any], demographics: dict[str, Any]
) -> Features:
    """Stage 1 — assemble the feature vector. Deterministic, no model."""
    zones = {z["id"]: z for z in hemo.get("zones", [])} if hemo.get("zones") else {}
    sac = zones.get("dome") or zones.get("sac") or {}
    return Features({
        "tawss_sac_pa": float(sac.get("tawss", 0.0)),
        "osi_sac": float(sac.get("osi", 0.0)),
        "rrt_sac": float(sac.get("rrt", 0.0)),
        "ecap_sac": float(sac.get("ecap", 0.0)),
        "nwss": float(hemo.get("nwss", 0.0)),
        "lsar_relative": float(hemo.get("lsar_relative", 0.0)),
        "max_diameter_mm": float(morphology.get("maxDiameter", morphology.get("max_diameter_mm", 0.0))),
        "aspect_ratio": float(morphology.get("aspectRatio", morphology.get("aspect_ratio", 0.0))),
        "dome_to_neck": float(morphology.get("domeToNeck", morphology.get("dome_to_neck", 0.0))),
        "non_sphericity_index": float(morphology.get("nonSphericityIndex",
                                                    morphology.get("non_sphericity_index", 0.0))),
        "age": float(demographics.get("age", 60)),
        "hypertension": 1.0 if demographics.get("hypertension") else 0.0,
        "earlier_sah": 1.0 if demographics.get("earlierSAH", demographics.get("earlier_sah")) else 0.0,
        "site_score": SITE_SCORE.get(demographics.get("site", "ICA"), 0.0),
    })


def _synthesise_cohort(n: int = 4000, seed: int = 20260804) -> tuple[np.ndarray, np.ndarray]:
    """
    Build a synthetic training cohort from published risk relationships.

    The generative model encodes, as a log-odds sum:
      * low sac TAWSS and high OSI increase risk (Meng, Xiang)
      * larger size and higher aspect ratio increase risk (PHASES; Ujiie)
      * age >= 70, hypertension, prior SAH, and posterior/communicating site
        increase risk (PHASES point weights)
    Noise is added so the label is not a deterministic function of the features
    — otherwise the model would simply invert the formula and report a perfect,
    meaningless AUC.
    """
    rng = np.random.default_rng(seed)

    tawss = rng.gamma(2.0, 0.35, n)
    osi = np.clip(rng.beta(2.0, 8.0, n) * 0.5, 0, 0.5)
    nwss = np.clip(tawss / rng.uniform(1.5, 4.0, n), 0.01, 2.0)
    rrt = 1.0 / np.maximum(0.02, (1 - 2 * osi) * tawss)
    ecap = osi / np.maximum(0.02, tawss)
    lsar = np.clip(rng.beta(2.0, 2.0, n), 0, 1)
    diameter = np.clip(rng.gamma(3.0, 1.9, n), 1.5, 25.0)
    ar = np.clip(rng.gamma(3.0, 0.42, n), 0.4, 3.5)
    dn = np.clip(ar * rng.uniform(0.9, 1.6, n), 0.4, 4.0)
    nsi = np.clip(rng.beta(2.0, 6.0, n), 0, 0.6)
    age = np.clip(rng.normal(58, 13, n), 20, 90)
    htn = (rng.random(n) < 0.42).astype(float)
    sah = (rng.random(n) < 0.08).astype(float)
    site = rng.choice([0.0, 2.0, 4.0], n, p=[0.45, 0.35, 0.20])

    logit = (
        -3.1
        + 1.35 * np.clip((0.4 - tawss) / 0.4, 0, 1)      # low shear
        + 1.15 * np.clip((osi - 0.2) / 0.3, 0, 1)        # oscillatory shear
        + 0.85 * np.clip(lsar - 0.4, 0, 1)
        + 0.95 * np.clip((diameter - 7.0) / 13.0, 0, 1)  # PHASES size bands
        + 0.80 * np.clip((ar - 1.3) / 2.0, 0, 1)
        + 0.45 * np.clip(nsi / 0.4, 0, 1)
        + 0.30 * (age >= 70)
        + 0.35 * htn
        + 0.55 * sah
        + 0.22 * site
        + rng.normal(0, 0.55, n)                          # irreducible noise
    )
    p = 1.0 / (1.0 + np.exp(-logit))
    y = (rng.random(n) < p).astype(int)

    X = np.column_stack([tawss, osi, rrt, ecap, nwss, lsar, diameter, ar, dn, nsi,
                         age, htn, sah, site])
    return X, y


def train(model_dir: Path, n: int = 4000) -> dict[str, Any]:
    """Stage 2 (offline) — fit and honestly evaluate the model."""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score, brier_score_loss
    from sklearn.model_selection import StratifiedKFold

    X, y = _synthesise_cohort(n)
    aucs, briers = [], []
    # Cross-validated, so the reported AUC is out-of-sample rather than the
    # meaningless in-sample number.
    for tr, te in StratifiedKFold(5, shuffle=True, random_state=0).split(X, y):
        m = lgb.LGBMClassifier(
            n_estimators=250, learning_rate=0.05, num_leaves=15,
            min_child_samples=40, subsample=0.85, colsample_bytree=0.85,
            reg_lambda=1.0, verbose=-1,
        )
        m.fit(X[tr], y[tr])
        p = m.predict_proba(X[te])[:, 1]
        aucs.append(roc_auc_score(y[te], p))
        briers.append(brier_score_loss(y[te], p))

    final = lgb.LGBMClassifier(
        n_estimators=250, learning_rate=0.05, num_leaves=15,
        min_child_samples=40, subsample=0.85, colsample_bytree=0.85,
        reg_lambda=1.0, verbose=-1,
    )
    final.fit(X, y)

    model_dir.mkdir(parents=True, exist_ok=True)
    final.booster_.save_model(str(model_dir / f"{MODEL_VERSION}.txt"))
    meta = {
        "model_version": MODEL_VERSION,
        "features": FEATURE_NAMES,
        "n_train": int(n),
        "prevalence": float(y.mean()),
        "cv_auc_mean": float(np.mean(aucs)),
        "cv_auc_std": float(np.std(aucs)),
        "cv_brier_mean": float(np.mean(briers)),
        "training_data": "SYNTHETIC — generated from published risk relationships",
        "clinical_validity": (
            "ILLUSTRATIVE ONLY. Not trained or validated on patient data. "
            "Demonstrates the feature/inference/explainability pipeline. "
            "Must not be used for clinical decision-making."
        ),
    }
    (model_dir / f"{MODEL_VERSION}.json").write_text(json.dumps(meta, indent=2))
    return meta


def predict(features: Features, model_dir: Path) -> dict[str, Any]:
    """Stage 2 (online) — inference plus SHAP attribution."""
    import lightgbm as lgb

    booster = lgb.Booster(model_file=str(model_dir / f"{MODEL_VERSION}.txt"))
    meta = json.loads((model_dir / f"{MODEL_VERSION}.json").read_text())
    x = features.vector()
    prob = float(booster.predict(x)[0])

    # SHAP via LightGBM's exact tree attribution — no sampling approximation,
    # and no extra dependency at inference time.
    contribs = booster.predict(x, pred_contrib=True)[0]
    shap = sorted(
        [{"feature": n, "value": float(features.values.get(n, 0.0)),
          "contribution": float(c)}
         for n, c in zip(FEATURE_NAMES, contribs[:-1])],
        key=lambda d: abs(d["contribution"]), reverse=True,
    )

    # Confidence from distance to the decision boundary, not from the
    # probability itself — a 0.5 output is maximally uncertain, not "50% sure".
    confidence = float(min(1.0, abs(prob - 0.5) * 2.0))
    category = "High" if prob >= 0.66 else ("Moderate" if prob >= 0.33 else "Low")

    return {
        "model_version": MODEL_VERSION,
        "probability": prob,
        "risk_category": category,
        "confidence": confidence,
        "expected_value": float(contribs[-1]),
        "shap": shap[:8],
        "cv_auc": meta.get("cv_auc_mean"),
        "clinical_validity": meta["clinical_validity"],
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Train or run the rupture-risk model")
    ap.add_argument("--model-dir", default="/mnt/d/fyp/services/worker/models")
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    md = Path(args.model_dir)
    if args.train:
        print(json.dumps(train(md), indent=2))
    if args.demo:
        f = extract_features(
            {"zones": [{"id": "dome", "tawss": 0.236, "osi": 0.31, "rrt": 11.0, "ecap": 1.3}],
             "nwss": 0.08, "lsar_relative": 0.85},
            {"maxDiameter": 8.0, "aspectRatio": 1.9, "domeToNeck": 1.6,
             "nonSphericityIndex": 0.19},
            {"age": 72, "hypertension": True, "earlierSAH": False, "site": "MCA"},
        )
        print(json.dumps(predict(f, md), indent=2))
