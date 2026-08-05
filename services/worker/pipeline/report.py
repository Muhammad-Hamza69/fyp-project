"""
Clinical PDF report generation.

Rendered with matplotlib's PDF backend rather than WeasyPrint/ReportLab: the
report's most valuable content is *plots of real data* (wall shear stress
distribution, SHAP attribution, risk decomposition), matplotlib is already a
dependency via VTK, and it needs no system Pango/Cairo stack — which matters
because the report service is meant to run on a small cloud instance rather
than the CFD worker.

Every figure is drawn from computed values. Nothing on the page is decorative.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages

# Matches the dashboard's clinical palette so print and screen agree.
C_HIGH, C_MOD, C_LOW = "#B83232", "#C47D1A", "#1E8C4E"
C_TEXT, C_MUTED, C_ACCENT = "#1e293b", "#64748b", "#2563eb"

# Mirrors thresholds.js. OSI_HIGH is 0.030, not 0.2: the reported value is an
# area-weighted sac mean and 0.2 is a peak-OSI figure, so the report could
# never flag a case no matter what the solve produced.
THRESHOLDS = {"TAWSS_LOW_PA": 0.4, "OSI_HIGH": 0.030, "RRT_HIGH": 3.0, "ECAP_HIGH": 1.0}


def _tier_colour(tier: str) -> str:
    return {"High": C_HIGH, "Moderate": C_MOD, "Low": C_LOW}.get(tier, C_MUTED)


def _header(fig, title: str, subtitle: str = "") -> None:
    fig.text(0.06, 0.955, "NeuroFlow CFD Analyst", fontsize=15, fontweight="bold", color=C_TEXT)
    fig.text(0.06, 0.932, title, fontsize=10.5, color=C_ACCENT)
    if subtitle:
        fig.text(0.06, 0.912, subtitle, fontsize=8, color=C_MUTED)
    fig.lines.append(plt.Line2D([0.06, 0.94], [0.902, 0.902], transform=fig.transFigure,
                                color="#cbd5e1", lw=0.8))
    fig.text(0.06, 0.03,
             "Research software — not a medical device. Not for clinical decision-making.",
             fontsize=7, color=C_MUTED, style="italic")


def _page_summary(pdf: PdfPages, rec: dict[str, Any]) -> None:
    fig = plt.figure(figsize=(8.27, 11.69))   # A4 portrait
    tier = rec.get("riskTier", "—")
    _header(fig, "Clinical Hemodynamic Assessment",
            f"Case {rec['id']}  ·  generated {datetime.now():%Y-%m-%d %H:%M}")

    # --- headline risk ------------------------------------------------------
    ax = fig.add_axes([0.06, 0.70, 0.40, 0.17]); ax.axis("off")
    score = rec.get("riskBreakdown", {}).get("composite", 0)
    ax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ax.transAxes,
                               facecolor=_tier_colour(tier), alpha=0.10,
                               edgecolor=_tier_colour(tier), lw=1.4))
    ax.text(0.5, 0.66, f"{score}", ha="center", fontsize=42, fontweight="bold",
            color=_tier_colour(tier), transform=ax.transAxes)
    ax.text(0.5, 0.40, "Composite Risk Index  / 100", ha="center", fontsize=8.5,
            color=C_MUTED, transform=ax.transAxes)
    ax.text(0.5, 0.18, tier, ha="center", fontsize=13, fontweight="bold",
            color=_tier_colour(tier), transform=ax.transAxes)

    # The caveat belongs with the number it qualifies. OSI carries 30% of the
    # weighting, so a steady solve forfeits that share to a term nobody
    # evaluated and the headline score is a floor, not an estimate. Printing 49
    # unqualified invites it to be read as complete.
    if not rec.get("hemodynamics", {}).get("transient", True):
        ax.text(0.5, 0.04,
                "Lower bound — OSI (30%) not computed on this steady solve",
                ha="center", fontsize=6.5, color=C_MUTED, transform=ax.transAxes)

    # --- risk decomposition -------------------------------------------------
    ax = fig.add_axes([0.54, 0.70, 0.40, 0.17])
    b = rec.get("riskBreakdown", {})
    _transient = rec.get("hemodynamics", {}).get("transient", True)
    labels = ["TAWSS\n(35%)", "OSI\n(30%)", "Diameter\n(20%)", "Aspect\n(15%)"]
    vals = [b.get("tawssScore", 0), b.get("osiScore", 0),
            b.get("diameterScore", 0), b.get("aspectScore", 0)]
    bars = ax.barh(labels, vals, color=[C_HIGH, C_MOD, C_ACCENT, C_LOW], alpha=0.85, height=0.6)
    ax.set_xlim(0, 100); ax.set_xlabel("sub-score", fontsize=8)
    ax.tick_params(labelsize=7.5)
    ax.set_title("Risk contribution", fontsize=9, color=C_TEXT, pad=6)
    for i, (bar, v) in enumerate(zip(bars, vals)):
        # An empty OSI bar labelled "0" looks like a measured absence of
        # oscillatory shear rather than a term that was never evaluated.
        txt = "n/a" if (i == 1 and not _transient) else f"{v:.0f}"
        ax.text(min(v + 2, 92), bar.get_y() + bar.get_height() / 2, txt,
                va="center", fontsize=7,
                color=C_MUTED if txt == "n/a" else C_TEXT)
    for s in ("top", "right"): ax.spines[s].set_visible(False)

    # --- hemodynamics table -------------------------------------------------
    ax = fig.add_axes([0.06, 0.42, 0.88, 0.24]); ax.axis("off")
    ax.text(0, 1.0, "Hemodynamic parameters", fontsize=10, fontweight="bold", color=C_TEXT)
    h = rec.get("hemodynamics", {})
    zones = {z.get("name", ""): z for z in rec.get("zones", [])}
    dome = zones.get("Aneurysm Dome", {})
    parent = zones.get("Parent Artery Inlet", {})

    # OSI and ECAP exist only for a transient solve. On a steady one they are
    # zero by construction — the definition compares two averages of the same
    # field, and with a single flow state those are identical. Printing "0.000"
    # in a PDF is worse than on screen: the report is the artifact most likely
    # to be read on its own, where 0.000 alongside a "> 0.2" threshold reads
    # unambiguously as a measured pass.
    transient = h.get("transient", True)
    osi_val = f"{dome.get('osi', 0):.3f}" if transient else "not computed"
    ecap_val = f"{h.get('ecap', 0):.3f} Pa⁻¹" if transient else "not computed"
    cycle_thr = "> 0.2" if transient else "steady solve"
    ecap_thr = "> 1.0" if transient else "steady solve"

    rows = [
        ("TAWSS — aneurysm dome", f"{dome.get('tawss', 0):.3f} Pa", "< 0.4 Pa",
         dome.get("tawss", 1) < THRESHOLDS["TAWSS_LOW_PA"]),
        ("TAWSS — parent artery", f"{parent.get('tawss', 0):.3f} Pa", "reference", False),
        ("OSI — aneurysm dome", osi_val, cycle_thr,
         transient and dome.get("osi", 0) > THRESHOLDS["OSI_HIGH"]),
        ("RRT — aneurysm dome", f"{h.get('rrt', 0):.2f} Pa⁻¹", "> 3.0",
         h.get("rrt", 0) > THRESHOLDS["RRT_HIGH"]),
        ("ECAP — aneurysm dome", ecap_val, ecap_thr,
         transient and h.get("ecap", 0) > THRESHOLDS["ECAP_HIGH"]),
        ("NWSS (sac / parent)", f"{h.get('nwss', 0):.3f}", "—", False),
        ("LSAR (relative, <10% parent)", f"{h.get('lsarRelative', 0)*100:.1f} %", "—", False),
        ("LSAR (absolute, <0.4 Pa)", f"{h.get('lsarAbsolute', 0)*100:.1f} %", "—", False),
    ]
    y = 0.86
    ax.text(0.00, y, "Parameter", fontsize=7.5, fontweight="bold", color=C_MUTED)
    ax.text(0.46, y, "Value", fontsize=7.5, fontweight="bold", color=C_MUTED)
    ax.text(0.66, y, "Threshold", fontsize=7.5, fontweight="bold", color=C_MUTED)
    ax.text(0.86, y, "Status", fontsize=7.5, fontweight="bold", color=C_MUTED)
    y -= 0.06
    for label, val, thr, flagged in rows:
        # A row with no value has no status. Printing "normal" against
        # "not computed" is the same error as printing 0.000 — it turns an
        # absent measurement into a passing one, in the column a reader scans
        # first.
        computed = val != "not computed"
        if not computed:
            status, status_colour, status_weight = "—", C_MUTED, "normal"
        elif flagged:
            status, status_colour, status_weight = "FLAGGED", C_HIGH, "bold"
        else:
            status, status_colour, status_weight = "normal", C_LOW, "normal"

        ax.text(0.00, y, label, fontsize=8, color=C_TEXT)
        ax.text(0.46, y, val, fontsize=8,
                color=C_TEXT if computed else C_MUTED,
                fontweight="bold" if computed else "normal")
        ax.text(0.66, y, thr, fontsize=7.5, color=C_MUTED)
        ax.text(0.86, y, status, fontsize=7.5,
                color=status_colour, fontweight=status_weight)
        y -= 0.098

    # --- morphology ---------------------------------------------------------
    ax = fig.add_axes([0.06, 0.20, 0.88, 0.18]); ax.axis("off")
    ax.text(0, 1.0, "Morphology (measured from the reconstructed surface)",
            fontsize=10, fontweight="bold", color=C_TEXT)
    m = rec.get("morphology", {})
    items = [
        ("Max diameter", f"{m.get('maxDiameter', 0):.2f} mm"),
        ("Neck diameter", f"{m.get('neckDiameterMm', 0):.2f} mm"),
        ("Aspect ratio", f"{m.get('aspectRatio', 0):.2f}"),
        ("Dome-to-neck", f"{m.get('domeToNeck', 0):.2f}"),
        ("Volume", f"{m.get('volumeMm3', 0):.1f} mm³"),
        ("Surface area", f"{m.get('surfaceAreaMm2', 0):.1f} mm²"),
        ("Non-sphericity", f"{m.get('nonSphericityIndex', 0):.3f}"),
    ]
    for i, (k, v) in enumerate(items):
        col, row = i % 4, i // 4
        ax.text(col * 0.25, 0.72 - row * 0.34, k, fontsize=7.5, color=C_MUTED)
        ax.text(col * 0.25, 0.55 - row * 0.34, v, fontsize=9.5, color=C_TEXT, fontweight="bold")

    # --- provenance ---------------------------------------------------------
    ax = fig.add_axes([0.06, 0.06, 0.88, 0.11]); ax.axis("off")
    p = rec.get("provenance", {})
    ax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ax.transAxes,
                               facecolor="#f1f5f9", edgecolor="#cbd5e1", lw=0.6))
    ax.text(0.02, 0.78, "Provenance", fontsize=8.5, fontweight="bold",
            color=C_TEXT, transform=ax.transAxes)
    txt = (f"Solver: {p.get('solver','—')}   ·   Mesh: {str(p.get('meshCells','—')).strip()}   ·   "
           f"{p.get('convergence','—')}\n"
           f"{p.get('note','')}")
    ax.text(0.02, 0.42, txt, fontsize=6.8, color=C_MUTED, transform=ax.transAxes,
            va="center", wrap=True)

    pdf.savefig(fig); plt.close(fig)


def _page_analysis(pdf: PdfPages, rec: dict[str, Any], ai: dict[str, Any] | None) -> None:
    fig = plt.figure(figsize=(8.27, 11.69))
    _header(fig, "Zone Analysis and Model Explainability", f"Case {rec['id']}")

    # --- per-zone shear -----------------------------------------------------
    ax = fig.add_axes([0.10, 0.62, 0.82, 0.24])
    zones = rec.get("zones", [])
    names = [z.get("name", "").replace("Parent Artery ", "").replace("Aneurysm ", "") for z in zones]
    tawss = [z.get("tawss", 0) for z in zones]
    cols = [C_HIGH if t < THRESHOLDS["TAWSS_LOW_PA"] else C_LOW for t in tawss]
    ax.bar(names, tawss, color=cols, alpha=0.85, width=0.55)
    ax.axhline(THRESHOLDS["TAWSS_LOW_PA"], ls="--", lw=1.1, color=C_HIGH)
    ax.text(len(names) - 0.4, THRESHOLDS["TAWSS_LOW_PA"] * 1.15,
            "low-shear threshold 0.4 Pa", fontsize=7, color=C_HIGH, ha="right")
    ax.set_ylabel("TAWSS (Pa)", fontsize=8.5)
    ax.set_title("Time-averaged wall shear stress by vascular zone", fontsize=9.5, pad=8)
    ax.tick_params(labelsize=8)
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    for i, t in enumerate(tawss):
        ax.text(i, t + max(tawss) * 0.02, f"{t:.3f}", ha="center", fontsize=7.5, color=C_TEXT)

    # --- SHAP ---------------------------------------------------------------
    ax = fig.add_axes([0.10, 0.32, 0.82, 0.22])
    if ai and ai.get("shap"):
        s = ai["shap"][:8][::-1]
        labels = [d["feature"].replace("_", " ") for d in s]
        contrib = [d["contribution"] for d in s]
        ax.barh(labels, contrib,
                color=[C_HIGH if c > 0 else C_LOW for c in contrib], alpha=0.85, height=0.6)
        ax.axvline(0, color="#94a3b8", lw=0.8)
        ax.set_xlabel("SHAP contribution to log-odds of rupture", fontsize=8)
        ax.set_title(f"Model explainability — {ai.get('model_version','')} "
                     f"(p = {ai.get('probability',0):.3f}, {ai.get('risk_category','')}, "
                     f"confidence {ai.get('confidence', 0)*100:.0f}%)",
                     fontsize=9.5, pad=8)
        ax.tick_params(labelsize=7.5)
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)

        # OSI and ECAP are inputs to this model. On a steady solve they are
        # absent rather than zero, so the probability rests on a feature vector
        # with two holes in it — and the model was handed zeros for both, which
        # is a value, not a gap. That has to travel with the number.
        if ai.get("inputs_complete") is False:
            ax.text(0, -0.34,
                    "Incomplete input vector: OSI and ECAP were not computed for this "
                    "steady solve and entered the model as zero.",
                    transform=ax.transAxes, fontsize=6.8, color=C_MOD)
    else:
        ax.axis("off")
        ax.text(0.5, 0.5, "No model prediction available for this case",
                ha="center", fontsize=9, color=C_MUTED)

    # --- assessment ---------------------------------------------------------
    ax = fig.add_axes([0.06, 0.10, 0.88, 0.17]); ax.axis("off")
    ax.text(0, 1.0, "Assessment", fontsize=10, fontweight="bold", color=C_TEXT)
    txt = rec.get("clinicalAssessment", "")
    wrapped, line = [], ""
    for word in txt.split():
        if len(line) + len(word) > 105:
            wrapped.append(line); line = word
        else:
            line = f"{line} {word}".strip()
    wrapped.append(line)
    ax.text(0, 0.86, "\n".join(wrapped), fontsize=7.8, color=C_TEXT, va="top", linespacing=1.7)

    if ai:
        ax.text(0, -0.30, f"Model caveat: {ai.get('clinical_validity','')}",
                fontsize=6.8, color=C_HIGH, va="top", style="italic", wrap=True)

    pdf.savefig(fig); plt.close(fig)


def generate_report(rec: dict[str, Any], out_pdf: Path,
                    ai: dict[str, Any] | None = None) -> dict[str, Any]:
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(out_pdf) as pdf:
        _page_summary(pdf, rec)
        _page_analysis(pdf, rec, ai)
        d = pdf.infodict()
        d["Title"] = f"NeuroFlow Hemodynamic Report — {rec['id']}"
        d["Author"] = "NeuroFlow CFD Analyst"
        d["Subject"] = "Cerebral aneurysm rupture-risk assessment"
    return {"pdf": str(out_pdf), "bytes": out_pdf.stat().st_size, "pages": 2}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Generate a clinical PDF report")
    ap.add_argument("patients_json")
    ap.add_argument("--id", default=None)
    ap.add_argument("--out", default="./report.pdf")
    ap.add_argument("--model-dir", default="/mnt/d/fyp/services/worker/models")
    args = ap.parse_args()

    doc = json.loads(Path(args.patients_json).read_text())
    recs = doc["patients"]
    rec = next((r for r in recs if r["id"] == args.id), recs[0])

    ai_out = None
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from risk_model import extract_features, predict
        feats = extract_features(
            {"zones": [{"id": "dome",
                        "tawss": rec["zones"][3]["tawss"], "osi": rec["zones"][3]["osi"],
                        "rrt": rec["hemodynamics"]["rrt"], "ecap": rec["hemodynamics"]["ecap"]}],
             "nwss": rec["hemodynamics"]["nwss"],
             "lsar_relative": rec["hemodynamics"]["lsarRelative"]},
            rec["morphology"], rec["demographics"],
        )
        ai_out = predict(feats, Path(args.model_dir))
    except Exception as exc:
        print(f"(model prediction unavailable: {exc})")

    print(json.dumps(generate_report(rec, Path(args.out), ai_out), indent=2))
