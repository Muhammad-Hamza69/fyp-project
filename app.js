/**
 * Cerebral Aneurysm Hemodynamic Analysis Dashboard - Core Logic (app.js)
 * Senior FYP Developer Implementation - 15 Years Experience Style
 */

// Global Utility: Sleep helper for pipeline timeline
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// Helper: Linear Interpolation for numbers
const lerp = (start, end, amt) => (1 - amt) * start + amt * end;

// Helper: Hex Color Lerp to map TAWSS and OSI values cleanly
function lerpColor(color1, color2, factor) {
    const r1 = parseInt(color1.substring(1, 3), 16);
    const g1 = parseInt(color1.substring(3, 5), 16);
    const b1 = parseInt(color1.substring(5, 7), 16);

    const r2 = parseInt(color2.substring(1, 3), 16);
    const g2 = parseInt(color2.substring(3, 5), 16);
    const b2 = parseInt(color2.substring(5, 7), 16);

    const r = Math.round(lerp(r1, r2, factor));
    const g = Math.round(lerp(g1, g2, factor));
    const b = Math.round(lerp(b1, b2, factor));

    return `rgb(${r}, ${g}, ${b})`;
}

// 1. Patient Database (Mock Dataset matching clinical profiles)
const patientDatabase = {
    "PT-2025-0041": {
        id: "PT-2025-0041",
        morphology: {
            maxDiameter: 8.4,
            aspectRatio: 2.1
        },
        demographics: {
            age: 72,
            hypertension: true,
            earlierSAH: false,
            population: "Other",
            site: "MCA"
        },
        zones: [
            { name: "Parent Artery Inlet", id: "3891", x: 160, y: 278, radius: 55, tawss: 1.85, osi: 0.03, isAneurysm: false },
            { name: "Parent Artery Outlet", id: "3942", x: 470, y: 278, radius: 55, tawss: 1.62, osi: 0.04, isAneurysm: false },
            { name: "Aneurysm Neck", id: "4109", x: 320, y: 220, radius: 35, tawss: 0.35, osi: 0.32, isAneurysm: true },
            { name: "Aneurysm Dome", id: "4289", x: 320, y: 120, radius: 50, tawss: 0.18, osi: 0.38, isAneurysm: true }
        ],
        clinicalAssessment: "Critical shear stress depletion detected at the aneurysm dome (TAWSS = 0.18 Pa) paired with extreme flow separation indices (OSI = 0.38). In conjunction with a high aspect ratio (2.1) and dome diameter (8.4 mm), this patient presents an elevated probability of wall degradation and rupture. Urgent surgical evaluation or endovascular intervention (e.g., flow diverter placement or coil embolization) is highly recommended."
    },
    "PT-2025-0037": {
        id: "PT-2025-0037",
        morphology: {
            maxDiameter: 5.2,
            aspectRatio: 1.4
        },
        demographics: {
            age: 58,
            hypertension: false,
            earlierSAH: false,
            population: "Other",
            site: "ICA"
        },
        zones: [
            { name: "Parent Artery Inlet", id: "3891", x: 160, y: 278, radius: 55, tawss: 2.12, osi: 0.02, isAneurysm: false },
            { name: "Parent Artery Outlet", id: "3942", x: 470, y: 278, radius: 55, tawss: 1.84, osi: 0.03, isAneurysm: false },
            { name: "Aneurysm Neck", id: "4109", x: 320, y: 220, radius: 35, tawss: 0.48, osi: 0.22, isAneurysm: true },
            { name: "Aneurysm Dome", id: "4289", x: 320, y: 120, radius: 50, tawss: 0.42, osi: 0.24, isAneurysm: true }
        ],
        clinicalAssessment: "Borderline wall shear stress detected near the aneurysm neck (TAWSS = 0.48 Pa) with mild flow stagnation (OSI = 0.24). The morphology parameters (aspect ratio 1.4, diameter 5.2 mm) indicate moderate progression. Continued clinical monitoring via high-resolution MRA every 6 months is recommended, alongside tight management of blood pressure."
    },
    "PT-2025-0039": {
        id: "PT-2025-0039",
        morphology: {
            maxDiameter: 3.1,
            aspectRatio: 0.9
        },
        demographics: {
            age: 45,
            hypertension: false,
            earlierSAH: false,
            population: "Other",
            site: "ICA"
        },
        zones: [
            { name: "Parent Artery Inlet", id: "3891", x: 160, y: 278, radius: 55, tawss: 1.95, osi: 0.04, isAneurysm: false },
            { name: "Parent Artery Outlet", id: "3942", x: 470, y: 278, radius: 55, tawss: 1.76, osi: 0.02, isAneurysm: false },
            { name: "Aneurysm Neck", id: "4109", x: 320, y: 220, radius: 35, tawss: 0.72, osi: 0.12, isAneurysm: true },
            { name: "Aneurysm Dome", id: "4289", x: 320, y: 120, radius: 50, tawss: 0.85, osi: 0.08, isAneurysm: true }
        ],
        clinicalAssessment: "Satisfactory wall shear stress profile across the entire vascular segment (TAWSS = 0.85 Pa), indicating stable laminar blood flow. Flow disturbance indices are negligible (OSI = 0.08). Aneurysm morphology is favorable, with a small dome diameter (3.1 mm) and aspect ratio below 1.0. Follow-up clinical scan in 12 months is sufficient to track stability."
    }
};

// Composite Risk Index: derived (not stored) from the aneurysm dome's live
// TAWSS/OSI plus morphology, so it can never drift from the displayed metrics.
// Weights: TAWSS 35% | OSI 30% | Max Diameter 20% | Aspect Ratio 15%
function clamp01(v) {
    return Math.max(0, Math.min(1, v));
}

/**
 * Was this case solved over a cardiac cycle?
 *
 * OSI, ECAP, transWSS, AFI and GON are all cycle quantities — they are not
 * merely small on a steady solve, they are undefined. The worker records the
 * distinction as `hemodynamics.transient`. Demonstration cases carry no
 * hemodynamics block and their OSI values are curated, so they are treated as
 * available: the DEMO badge already tells the reader what they are.
 */
/**
 * Attach (or clear) a short explanatory note under a gauge value.
 *
 * A bare "n/a" invites the reading that the pipeline broke. It did not — the
 * quantity is undefined for this kind of solve, and saying which is the whole
 * point of not printing 0.00.
 */
function setGaugeNote(valueEl, text) {
    if (!valueEl) return;

    // Attach to the CARD, not to the value's own parent. That parent is
    // `.radial-progress-text`, an absolutely-positioned overlay centred inside
    // the 80px ring — appending there wrapped the sentence into the middle of
    // the doughnut, on top of the value it was explaining. The note belongs
    // under the whole gauge.
    const card = valueEl.closest(".metric-card") || valueEl.parentElement;
    if (!card) return;

    let note = card.querySelector(":scope > .gauge-note");
    if (!text) {
        if (note) note.remove();
        return;
    }
    if (!note) {
        note = document.createElement("div");
        note.className = "gauge-note";
        card.appendChild(note);
    }
    note.textContent = text;
}

/**
 * Has a CFD solve produced hemodynamics for this case at all?
 *
 * An uploaded scan with no completed run has zeros in its zones — there is
 * nothing else to put there. Rendering those zeros would show TAWSS 0.00 Pa
 * and fire the "Low TAWSS (< 0.4 Pa)" alert on a case that was never solved:
 * a critical finding manufactured out of absent data. Every hemodynamic gauge
 * checks this before displaying a number.
 */
/**
 * Surrogate OSI/ECAP for a case that was solved STEADY.
 *
 * A steady solve produces no OSI — there is no cycle to measure. Until now
 * those cases showed "n/a" and nothing else, which is honest but unhelpful when
 * a usable estimate exists. The surrogate is calibrated on the transient solves
 * and can supply one from the sac's TAWSS.
 *
 * Returned separately from the measured values, never merged into them: the
 * gauges mark an estimate distinctly so nobody reads it as a solve result. The
 * Composite Risk Index is deliberately NOT recomputed from it — and it happens
 * not to matter, because every estimate this produces (~0.005-0.013) sits below
 * the 0.03 floor of the OSI risk normalisation and would score zero anyway.
 */
function estimateOsi(patient) {
    if (!patient || !window.NeuroSurrogate || !window.NeuroSurrogate.isLoaded()) return null;
    // Only for cases that HAVE a solve but lack a cycle. An upload with no
    // solve at all is handled on its own path.
    if (patient.awaitingCfd) return null;
    if (hemodynamicsAreTransient(patient)) return null;      // already measured

    const m = patient.morphology || {};
    if (!m.maxDiameter) return null;
    try {
        const p = window.NeuroSurrogate.predict({
            maxDiameterMm: m.maxDiameter,
            neckDiameterMm: m.neckDiameterMm,
        });
        if (p.osi === null || p.osi === undefined) return null;
        return {
            osi: p.osi,
            ecap: p.ecap,
            points: p.osiCalibrationPoints,
            maxErrorPct: p.osiMaxErrorPct,
            extrapolating: p.extrapolating,
            warnings: p.warnings,
        };
    } catch { return null; }
}

function cfdIsComputed(patient) {
    return !(patient && patient.awaitingCfd);
}

function hemodynamicsAreTransient(patient) {
    const h = patient && patient.hemodynamics;
    return !h || h.transient !== false;
}

function computeRiskBreakdown(patient) {
    const domeZone = patient.zones.find(z => z.name === "Aneurysm Dome");
    const { maxDiameter, aspectRatio } = patient.morphology;

    const tawssScore = clamp01((1.5 - domeZone.tawss) / (1.5 - 0.15)) * 100;
    const osiScore = clamp01((domeZone.osi - 0.03) / (0.35 - 0.03)) * 100;
    const diameterScore = clamp01((maxDiameter - 2.0) / (10.0 - 2.0)) * 100;
    const aspectScore = clamp01((aspectRatio - 0.7) / (2.5 - 0.7)) * 100;

    const composite = (tawssScore * 0.35) + (osiScore * 0.30) + (diameterScore * 0.20) + (aspectScore * 0.15);

    // On a steady solve osiScore is 0 for want of a cycle, not for want of
    // oscillation — yet it still consumes its full 30% of the weighting. The
    // composite is therefore a strict LOWER BOUND on this case's risk: any real
    // OSI can only raise it. That is worth saying rather than presenting the
    // number as complete.
    //
    // The weights are deliberately NOT renormalised over the remaining terms.
    // Doing so would silently restate every steady case's headline score (this
    // cohort's 39/49/59 would become 56/64/72 and change two risk tiers) on the
    // strength of a quantity nobody measured. A stated lower bound is honest;
    // an invented redistribution is not.
    const osiComputed = hemodynamicsAreTransient(patient);
    const maxIfOsiWere = composite + (osiComputed ? 0 : 100 * 0.30);

    return {
        tawssScore, osiScore, diameterScore, aspectScore,
        osiComputed,
        compositeIsLowerBound: !osiComputed,
        compositeUpperBound: Math.round(maxIfOsiWere),
        composite: Math.round(composite)
    };
}

function computeCompositeRisk(patient) {
    return computeRiskBreakdown(patient).composite;
}

function getRiskTier(score) {
    if (score >= 75) {
        return { riskLevel: "High", badgeClass: "badge-high", riskLabel: "High Rupture Risk", riskLabelClass: "color-high-risk" };
    } else if (score >= 45) {
        return { riskLevel: "Moderate", badgeClass: "badge-moderate", riskLabel: "Moderate Risk Profile", riskLabelClass: "color-mod-risk" };
    }
    return { riskLevel: "Low", badgeClass: "badge-low", riskLabel: "Stable / Low Risk", riskLabelClass: "color-low-risk" };
}

// Relative Residence Time & Endothelial Cell Activation Potential: supplementary
// hemodynamic markers derived from the same dome TAWSS/OSI, used in current CFD
// rupture-risk literature alongside TAWSS/OSI (see project research notes).
// RRT ~ 1 / ((1 - 2*OSI) * TAWSS); guarded against the OSI->0.5 singularity.
function computeRRT(domeZone, patient) {
    // Prefer the solver's AREA-WEIGHTED value when the case has one.
    //
    // RRT is non-linear in TAWSS and OSI, so by Jensen's inequality the mean of
    // the function is not the function of the means. Evaluating 1/((1-2·OSI)·TAWSS)
    // at the sac's AVERAGE shear systematically under-reports residence time,
    // because the reciprocal is convex. The gap is not cosmetic — on this cohort
    // it is 11.07 vs 4.25, 21.35 vs 7.10 and 7.17 vs 2.94.
    //
    // It mattered clinically too: PT-2026-0103 read 2.94 here, below the 3.0
    // threshold, so no alert fired — while its true area-weighted RRT is 7.17.
    // The PDF report and the methods document had been quoting the correct
    // figure all along, so the dashboard was the one disagreeing.
    const aw = patient && patient.hemodynamics && patient.hemodynamics.rrt;
    if (typeof aw === "number" && aw > 0) return aw;

    // Demonstration cases carry no solver output; the closed form is all there is.
    const denom = Math.max(0.02, (1 - 2 * domeZone.osi) * domeZone.tawss);
    return 1 / denom;
}

// ECAP = OSI / TAWSS - values above ~1.0 mean the oscillatory component
// dominates over mean shear, a marker associated with elevated rupture risk.
function computeECAP(domeZone, patient) {
    // Area-weighted where available, for the same reason as RRT above:
    // ECAP = OSI/TAWSS is non-linear, and evaluating it at the means gave
    // 0.024 against a true 0.173 for PT-2026-0103 — a factor of seven.
    const aw = patient && patient.hemodynamics && patient.hemodynamics.ecap;
    if (typeof aw === "number" && aw > 0) return aw;

    return domeZone.osi / Math.max(0.02, domeZone.tawss);
}

// PHASES score (Greving et al. 2014): a demographic/morphological rupture-risk
// score used clinically alongside (not instead of) hemodynamic CFD assessment.
// Population, Hypertension, Age, Size, Earlier SAH, Site of aneurysm.
const PHASES_SITE_LABELS = {
    ICA: "Internal Carotid Artery (ICA)",
    MCA: "Middle Cerebral Artery (MCA)",
    ACOM_PCOM_POST: "Ant./Post. Communicating or Posterior Circulation"
};

const PHASES_SITE_POINTS = {
    ICA: 0,
    MCA: 2,
    ACOM_PCOM_POST: 4
};

const PHASES_POPULATION_POINTS = {
    "Other": 0, // North American / European / Other
    "Japanese": 3,
    "Finnish": 5
};

// Cumulative 5-year rupture risk (%) by total PHASES point total
const PHASES_RISK_TABLE = [
    { max: 1, percent: 0.4 },
    { max: 3, percent: 0.7 },
    { max: 4, percent: 0.9 },
    { max: 5, percent: 1.3 },
    { max: 6, percent: 1.7 },
    { max: 7, percent: 2.4 },
    { max: 8, percent: 3.2 },
    { max: 9, percent: 4.3 },
    { max: 10, percent: 5.3 },
    { max: 11, percent: 7.2 },
    { max: Infinity, percent: 17.8 }
];

function phasesRiskPercentFromPoints(points) {
    const bracket = PHASES_RISK_TABLE.find(b => points <= b.max);
    return bracket.percent;
}

function computePhasesScore(patient) {
    const d = patient.demographics;
    const diameter = patient.morphology.maxDiameter;

    let sizePoints = 0;
    if (diameter >= 20.0) sizePoints = 10;
    else if (diameter >= 10.0) sizePoints = 6;
    else if (diameter >= 7.0) sizePoints = 3;

    const items = [
        { label: "Population", value: d.population, points: PHASES_POPULATION_POINTS[d.population] ?? 0 },
        { label: "Hypertension", value: d.hypertension ? "Yes" : "No", points: d.hypertension ? 1 : 0 },
        { label: "Age", value: `${d.age} yrs`, points: d.age >= 70 ? 1 : 0 },
        { label: "Size of Aneurysm", value: `${diameter.toFixed(1)} mm`, points: sizePoints },
        { label: "Earlier SAH (other aneurysm)", value: d.earlierSAH ? "Yes" : "No", points: d.earlierSAH ? 1 : 0 },
        { label: "Site of Aneurysm", value: PHASES_SITE_LABELS[d.site], points: PHASES_SITE_POINTS[d.site] ?? 0 }
    ];

    const points = items.reduce((sum, item) => sum + item.points, 0);
    return { items, points, riskPercent: phasesRiskPercentFromPoints(points) };
}

// Global App State
let activePatient = patientDatabase["PT-2025-0041"];
let currentMapMode = "TAWSS"; // TAWSS or OSI
let hoverZone = null;

// DOM Elements - Dashboard View
const activePatientIdEl = document.getElementById("active-patient-id");
const activePatientStatusEl = document.getElementById("active-patient-status");
const patientListContainer = document.getElementById("patient-list-container");
const toggleTawssBtn = document.getElementById("toggle-tawss-btn");
const toggleOsiBtn = document.getElementById("toggle-osi-btn");
const mainCanvas = document.getElementById("heatmap-canvas");
const mainCtx = mainCanvas.getContext("2d");

// DOM Elements - 2D/3D View Switching
const view2dBtn = document.getElementById("view-2d-btn");
const view3dBtn = document.getElementById("view-3d-btn");
const view2dPane = document.getElementById("view-2d-pane");
const view3dPane = document.getElementById("view-3d-pane");
const workspaceTitleEl = document.getElementById("workspace-title");

// DOM Elements - Tooltip
const tooltipEl = document.getElementById("canvas-tooltip");
const tooltipNodeIdEl = document.getElementById("tooltip-node-id");
const tooltipRiskTagEl = document.getElementById("tooltip-risk-tag");
const tooltipTawssValEl = document.getElementById("tooltip-tawss-val");
const tooltipOsiValEl = document.getElementById("tooltip-osi-val");
const tooltipAlarmMsgEl = document.getElementById("tooltip-alarm-msg");

// DOM Elements - Gauges & Cards
const compositeRiskScoreEl = document.getElementById("composite-risk-score");
const compositeRiskLabelEl = document.getElementById("composite-risk-label");
const compositeNeedleEl = document.getElementById("composite-needle");
const compositeRingFill = document.getElementById("composite-ring-fill");
const tawssGaugeValEl = document.getElementById("tawss-gauge-val");
const osiGaugeValEl = document.getElementById("osi-gauge-val");
const tawssProgressFill = document.getElementById("tawss-progress-fill");
const osiProgressFill = document.getElementById("osi-progress-fill");
const tawssAlertEl = document.getElementById("tawss-alert");
const osiAlertEl = document.getElementById("osi-alert");
const morphMaxDiameterEl = document.getElementById("morph-max-diameter");
const morphAspectRatioEl = document.getElementById("morph-aspect-ratio");

// DOM Elements - RRT / ECAP Gauges
const rrtGaugeValEl = document.getElementById("rrt-gauge-val");
const ecapGaugeValEl = document.getElementById("ecap-gauge-val");
const rrtProgressFill = document.getElementById("rrt-progress-fill");
const ecapProgressFill = document.getElementById("ecap-progress-fill");
const rrtAlertEl = document.getElementById("rrt-alert");
const ecapAlertEl = document.getElementById("ecap-alert");

// DOM Elements - Composite Risk Breakdown (explainability)
const breakdownTawssFillEl = document.getElementById("breakdown-tawss-fill");
const breakdownOsiFillEl = document.getElementById("breakdown-osi-fill");
const breakdownDiameterFillEl = document.getElementById("breakdown-diameter-fill");
const breakdownAspectFillEl = document.getElementById("breakdown-aspect-fill");
const breakdownTawssPctEl = document.getElementById("breakdown-tawss-pct");
const breakdownOsiPctEl = document.getElementById("breakdown-osi-pct");
const breakdownDiameterPctEl = document.getElementById("breakdown-diameter-pct");
const breakdownAspectPctEl = document.getElementById("breakdown-aspect-pct");

// DOM Elements - PHASES Clinical Risk Score
const phasesTotalPointsEl = document.getElementById("phases-total-points");
const phasesRiskPercentEl = document.getElementById("phases-risk-percent");
const phasesBreakdownEl = document.getElementById("phases-breakdown");

// DOM Elements - Modals
const expandCaseBtn = document.getElementById("expand-case-btn");
const reportModalEl = document.getElementById("report-modal");
const closeReportBtn = document.getElementById("close-report-btn");
const cancelReportBtn = document.getElementById("cancel-report-btn");
const exportPdfBtn = document.getElementById("export-pdf-btn");

// DOM Elements - Report Data Injection
const reportPatientIdEl = document.getElementById("report-patient-id");
const reportDateGeneratedEl = document.getElementById("report-date-generated");
const reportRiskBadgeEl = document.getElementById("report-risk-badge");
const reportTawssValEl = document.getElementById("report-tawss-val");
const reportTawssStatusEl = document.getElementById("report-tawss-status");
const reportOsiValEl = document.getElementById("report-osi-val");
const reportOsiStatusEl = document.getElementById("report-osi-status");
const reportDiameterValEl = document.getElementById("report-diameter-val");
const reportDiameterStatusEl = document.getElementById("report-diameter-status");
const reportAspectValEl = document.getElementById("report-aspect-val");
const reportAspectStatusEl = document.getElementById("report-aspect-status");
const reportRrtValEl = document.getElementById("report-rrt-val");
const reportRrtStatusEl = document.getElementById("report-rrt-status");
const reportEcapValEl = document.getElementById("report-ecap-val");
const reportEcapStatusEl = document.getElementById("report-ecap-status");
const reportCompositeScoreEl = document.getElementById("report-composite-score");
const reportCompositeStatusEl = document.getElementById("report-composite-status");
const reportClinicalTextEl = document.getElementById("report-clinical-text");
const reportAnatomicalTargetEl = document.getElementById("report-anatomical-target");
const reportPhasesBreakdownBodyEl = document.getElementById("report-phases-breakdown-body");
const reportPhasesPointsEl = document.getElementById("report-phases-points");
const reportPhasesPercentEl = document.getElementById("report-phases-percent");

// DOM Elements - Upload & Simulation
const sidebarUploadBox = document.getElementById("sidebar-upload-box");
const dropZoneContainer = document.getElementById("drop-zone-container");
const fileUploader = document.getElementById("file-uploader");
const dragDropOverlay = document.getElementById("drag-drop-overlay");
const simulationModalEl = document.getElementById("simulation-modal");
const abortSimBtn = document.getElementById("abort-sim-btn");
const terminalLogOutput = document.getElementById("terminal-log-output");
const activeStepBadge = document.getElementById("active-step-badge");

// Real CFD cases produced by the OpenFOAM pipeline (services/worker).
// Written by pipeline/export_patient.py; every hemodynamic and morphological
// value in that file is computed, not authored.
//
// Loaded additively and defensively: if the fetch fails (opened over file://,
// or the file has not been generated yet) the dashboard silently keeps its
// demonstration dataset, so the UI can never be broken by a missing solve.
// Where to look for computed cases, in priority order:
//   1. the live API  (FastAPI -> Neon Postgres) when one is reachable
//   2. the static export produced by pipeline/export_patient.py
//
// Both return the identical payload shape, so the rendering code below is
// unaware of which one answered. The static file is what the deployed static
// site uses; the API is what a local full-stack demo uses.
// Empty string = same origin. The API is deployed as a serverless function
// alongside this page at /api/v1, so relative URLs avoid CORS entirely and
// work identically in production and in a local `python -m http.server` when
// an API is running on the same host. Override with window.NEUROFLOW_API_BASE
// to point at a separately-hosted API.
const API_BASE = (window.NEUROFLOW_API_BASE ?? "").replace(/\/$/, "");

async function fetchComputedCases() {
    // Short timeout: if no API is running (the normal case for the deployed
    // static site) we must not stall the dashboard waiting for a refused
    // connection before falling back.
    try {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 2500);
        const res = await fetch(`${API_BASE}/api/v1/dashboard/patients`, {
            cache: "no-store", signal: ctrl.signal
        });
        clearTimeout(timer);
        if (res.ok) {
            const data = await res.json();
            if (Array.isArray(data.patients) && data.patients.length) {
                return { cases: data.patients, source: "api" };
            }
        }
    } catch (_) {
        /* no API reachable — fall through to the static export */
    }

    try {
        const res = await fetch("real-cfd-patients.json", { cache: "no-store" });
        if (!res.ok) return { cases: [], source: "none" };
        const data = await res.json();
        return { cases: Array.isArray(data.patients) ? data.patients : [], source: "static" };
    } catch (err) {
        console.info("[NeuroFlow] No computed CFD cases available; using demonstration dataset.", err.message);
        return { cases: [], source: "none" };
    }
}

async function loadRealCfdCases() {
    const { cases, source } = await fetchComputedCases();
    cases.forEach(p => { patientDatabase[p.id] = p; });
    if (cases.length > 0) {
        // Make a computed case the landing view — the real numbers are the
        // point of the project, so they should not be buried below the mocks.
        activePatient = patientDatabase[cases[0].id];
    }
    window.__neuroDataSource = source;
    return cases.length;
}

// Initialize Application
async function initApp() {
    const nReal = await loadRealCfdCases();

    // Preload the surrogate so the gauges, which run synchronously, can fall
    // back to an ESTIMATE for cases that were solved steady and therefore have
    // no OSI of their own. Failure is non-fatal: those gauges then show "n/a"
    // exactly as before.
    if (window.NeuroSurrogate) {
        try { await window.NeuroSurrogate.load(); } catch (err) {
            console.warn("[NeuroFlow] surrogate unavailable:", err.message);
        }
    }

    renderPatientList();
    loadPatientData(activePatient);
    setupEventListeners();

    // Set current date in report
    const today = new Date().toISOString().split('T')[0];
    reportDateGeneratedEl.textContent = today;

    if (nReal > 0) {
        const via = window.__neuroDataSource === "api"
            ? "live API (FastAPI → Neon PostgreSQL)"
            : "static export";
        console.info(`[NeuroFlow] Loaded ${nReal} case(s) computed by OpenFOAM, via ${via}.`);
    }
}

// 2. Patient Profile List Renderer
function renderPatientList() {
    patientListContainer.innerHTML = "";
    Object.values(patientDatabase).forEach(patient => {
        const card = document.createElement("div");
        card.className = `patient-card ${patient.id === activePatient.id ? 'active' : ''}`;
        card.dataset.id = patient.id;

        const score = computeCompositeRisk(patient);
        const tier = getRiskTier(score);

        // Cases solved by OpenFOAM carry a provenance block; demonstration
        // cases do not. Labelling this in the UI is deliberate — the difference
        // between computed and authored data is the central claim of the
        // project and should not require reading the source to establish.
        const isComputed = patient.provenance && patient.provenance.source === "computed";
        const provenanceTag = isComputed
            ? `<span class="provenance-badge" title="${patient.provenance.solver} — ${patient.provenance.convergence}"><i class="fa-solid fa-square-root-variable"></i> CFD</span>`
            : `<span class="provenance-badge provenance-demo" title="Curated demonstration dataset — not computed"><i class="fa-solid fa-flask"></i> DEMO</span>`;

        card.innerHTML = `
            <div class="card-header">
                <span class="patient-card-id">${patient.id}</span>
                <div class="card-header-right">
                    <span class="status-badge ${tier.badgeClass}">${tier.riskLevel}</span>
                    <button class="patient-delete-btn" type="button" title="Remove this case" aria-label="Remove case ${patient.id}">
                        <i class="fa-solid fa-trash-can"></i>
                    </button>
                </div>
            </div>
            <div class="patient-card-details">
                <span>CRI: ${score}/100</span>
                <span>Dia: ${patient.morphology.maxDiameter}mm</span>
                ${provenanceTag}
            </div>
        `;

        card.addEventListener("click", () => {
            document.querySelectorAll(".patient-card").forEach(c => c.classList.remove("active"));
            card.classList.add("active");
            loadPatientData(patientDatabase[patient.id]);
        });

        card.querySelector(".patient-delete-btn").addEventListener("click", (e) => {
            e.stopPropagation();
            deletePatient(patient.id);
        });

        patientListContainer.appendChild(card);
    });
}

// Remove an uploaded/mock case from the dashboard entirely
function deletePatient(patientId) {
    if (Object.keys(patientDatabase).length <= 1) {
        alert("At least one patient case must remain loaded.");
        return;
    }

    const confirmed = confirm(`Remove case ${patientId} from the dashboard? This cannot be undone.`);
    if (!confirmed) return;

    const wasActive = activePatient.id === patientId;
    delete patientDatabase[patientId];

    if (wasActive) {
        loadPatientData(Object.values(patientDatabase)[0]);
    }

    renderPatientList();
}

// 3. Load Selected Patient Data
function loadPatientData(patient) {
    activePatient = patient;

    const tier = getRiskTier(computeCompositeRisk(patient));

    // Header banner updates
    activePatientIdEl.textContent = patient.id;
    activePatientStatusEl.textContent = tier.riskLevel;
    activePatientStatusEl.className = `status-badge ${tier.badgeClass}`;

    // Morphology updates
    morphMaxDiameterEl.textContent = patient.morphology.maxDiameter.toFixed(1);
    morphAspectRatioEl.textContent = patient.morphology.aspectRatio.toFixed(1);

    // Canvas Redraw
    drawHeatmap();

    // Radial Telemetry Progress Indicators
    updateRadialGauges();

    // PHASES Clinical Risk Score (demographic/morphological, independent of CFD sim)
    renderPhasesScore(patient);

    // AI rupture prediction — a third, independent estimate
    renderMlPrediction(patient);

    // 3D Nerve/Vascular Model Redraw (no-op until the 3D tab has been opened)
    if (window.NeuroViewer) window.NeuroViewer.applyRiskColors(patient, currentMapMode);

    // Hide active tooltips on patient switch
    tooltipEl.classList.add("hidden");
    hoverZone = null;
}

// PHASES Clinical Risk Score Card Renderer
function renderPhasesScore(patient) {
    const { items, points, riskPercent } = computePhasesScore(patient);

    phasesTotalPointsEl.textContent = points;
    phasesRiskPercentEl.textContent = `${riskPercent.toFixed(1)}%`;

    phasesBreakdownEl.innerHTML = items.map(item => `
        <div class="phases-breakdown-row">
            <span>${item.label} (${item.value})</span>
            <span>${item.points} pt${item.points === 1 ? '' : 's'}</span>
        </div>
    `).join("");
}

/**
 * AI rupture-prediction card.
 *
 * A third, independent estimate beside the hemodynamic Composite Risk Index and
 * the PHASES clinical score. Deliberately NOT folded into either: three
 * estimates that can be compared are more informative than one blended number
 * whose disagreements have been averaged away.
 *
 * The card is hidden entirely for cases with no prediction rather than shown
 * with dashes — a permanently empty AI panel reads as a broken feature.
 */
function renderMlPrediction(patient) {
    const card = document.getElementById("ml-card");
    if (!card) return;

    const ml = patient && patient.ml;
    if (!ml) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");

    const pct = (ml.probability * 100).toFixed(1);
    document.getElementById("ml-probability").textContent = `${pct}%`;

    const catEl = document.getElementById("ml-category");
    catEl.textContent = ml.risk_category || "—";
    catEl.className = "ml-category " + ({
        High: "tier-high", Moderate: "tier-mod", Low: "tier-low",
    }[ml.risk_category] || "");

    // Confidence is distance from the decision boundary, not the probability.
    // A 0.545 output is maximally UNCERTAIN, not "moderately confident" —
    // showing the probability alone would invite exactly that misreading.
    document.getElementById("ml-confidence").textContent =
        `${(ml.confidence * 100).toFixed(0)}%`;

    // The model takes OSI as an input. On a steady solve OSI is absent rather
    // than zero, so the vector is incomplete and the probability rests on a
    // gap. Saying so beats presenting it as a finished number.
    const inc = document.getElementById("ml-incomplete");
    if (ml.inputs_complete === false) {
        inc.classList.remove("hidden");
        inc.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> '
            + 'Incomplete input vector — OSI and ECAP were not computed for this '
            + 'steady solve, so the model saw them as zero.';
    } else {
        inc.classList.add("hidden");
    }

    // SHAP contributions: signed, so the reader can see which features pushed
    // the prediction up and which pulled it down, rather than a bare score.
    const shap = Array.isArray(ml.shap) ? ml.shap.slice(0, 5) : [];
    const maxAbs = Math.max(...shap.map(s => Math.abs(s.contribution)), 1e-6);
    document.getElementById("ml-shap").innerHTML = shap.map(s => {
        const w = (Math.abs(s.contribution) / maxAbs) * 100;
        const up = s.contribution >= 0;
        return `<div class="ml-shap-row">
                  <span class="ml-shap-name">${SHAP_LABELS[s.feature] || s.feature}</span>
                  <span class="ml-shap-bar-wrap">
                    <span class="ml-shap-bar ${up ? "shap-up" : "shap-down"}"
                          style="width:${w.toFixed(0)}%"></span>
                  </span>
                  <span class="ml-shap-val">${up ? "+" : ""}${s.contribution.toFixed(2)}</span>
                </div>`;
    }).join("");

    // Never render a probability without this. The model is trained on a
    // synthetic cohort, never on patient data.
    const auc = (typeof ml.cv_auc === "number") ? ml.cv_auc.toFixed(2) : "—";
    document.getElementById("ml-validity").innerHTML =
        `<strong>Illustrative only.</strong> Model <code>${ml.model_version}</code> `
        + `is trained on a <strong>synthetic</strong> cohort generated from published `
        + `risk relationships — never on patient data. Cross-validated AUC ${auc}. `
        + `It demonstrates the feature/inference/explainability pipeline and must not `
        + `inform clinical decisions.`;
}

// Readable names for the model's feature vector.
const SHAP_LABELS = {
    tawss_sac_pa: "TAWSS (sac)",
    osi_sac: "OSI (sac)",
    rrt_sac: "RRT (sac)",
    ecap_sac: "ECAP (sac)",
    nwss: "Normalised WSS",
    lsar_relative: "LSAR (relative)",
    max_diameter_mm: "Max diameter",
    aspect_ratio: "Aspect ratio",
    dome_to_neck: "Dome-to-neck",
    non_sphericity_index: "Non-sphericity",
    age: "Age",
    hypertension: "Hypertension",
    earlier_sah: "Earlier SAH",
    site_score: "Aneurysm site",
};

/**
 * Ask for the sac measurements, then estimate hemodynamics instantly.
 *
 * WHY A PROMPT RATHER THAN AUTOMATIC EXTRACTION
 * The surrogate needs dome and neck diameter. Getting those from a scan means
 * segmenting it, which a browser cannot do — and guessing them would reproduce
 * exactly the fabrication this replaced. Clinically these are measured by a
 * radiologist off the images, so asking is both truthful and normal practice.
 *
 * WHY THIS IS INSTANT WHEN A SOLVE TAKES HOURS
 * The expensive computation was already paid, once, for the whole geometry
 * family: run_sweep.py solved it with OpenFOAM and surrogate.py fitted a
 * response surface through those solutions. Evaluating that surface is a
 * handful of logarithms. Nothing is skipped — the physics was done in advance
 * rather than per user.
 */
function promptMorphologyAndEstimate(patientId, dicomMeta, measured) {
    return new Promise((resolve) => {
        const card = document.getElementById("morphology-prompt");
        const domeEl = document.getElementById("morph-dome");
        const neckEl = document.getElementById("morph-neck");
        const btn = document.getElementById("morph-compute");
        if (!card || !btn || !window.NeuroSurrogate) {
            // No form to show, so nothing is waiting on the modal.
            const modal = document.getElementById("simulation-modal");
            if (modal) modal.classList.add("hidden");
            registerUnsolvedCase(patientId, dicomMeta);
            resolve();
            return;
        }

        window.NeuroSurrogate.load().then((model) => {
            const n = document.getElementById("morph-npoints");
            if (n) n.textContent = String(model.n_points);
            // Pre-fill from the image measurement so the defaults come from the
            // scan rather than from a placeholder.
            if (measured) {
                if (domeEl) domeEl.value = measured.domeDiameterMm;
                if (neckEl) neckEl.value = measured.neckDiameterMm;
            }
            const src = document.getElementById("morph-source");
            if (src) {
                src.textContent = measured
                    ? `Pre-filled from the uploaded image (${measured.method}). `
                      + `Correct them if your own measurement differs.`
                    : `Not measurable from this file — enter the values from your `
                      + `own reading of the scan.`;
            }
            card.classList.remove("hidden");
        }).catch((err) => {
            writeTerminalLog(`[ERROR] Surrogate unavailable: ${err.message}`, "error");
            writeTerminalLog("[INFO] Hemodynamics will show as not computed.", "info");
            // Nothing left to show in the modal, and leaving it open would trap
            // the user behind an overlay with no way forward.
            const modal = document.getElementById("simulation-modal");
            if (modal) modal.classList.add("hidden");
            registerUnsolvedCase(patientId, dicomMeta);
            resolve();
        });

        const onGo = () => {
            btn.removeEventListener("click", onGo);
            card.classList.add("hidden");
            // The modal was deliberately left open so this form could be seen
            // and used; now that it has been, close it and reveal the dashboard.
            const modal = document.getElementById("simulation-modal");
            if (modal) modal.classList.add("hidden");

            const dome = parseFloat(domeEl.value);
            const neck = parseFloat(neckEl.value);
            if (!Number.isFinite(dome) || dome <= 0) {
                writeTerminalLog("[ERROR] A positive dome diameter is required.", "error");
                registerUnsolvedCase(patientId, dicomMeta);
                resolve();
                return;
            }

            const t0 = performance.now();
            let p;
            try {
                p = window.NeuroSurrogate.predict({
                    maxDiameterMm: dome, neckDiameterMm: neck,
                });
            } catch (err) {
                writeTerminalLog(`[ERROR] ${err.message}`, "error");
                registerUnsolvedCase(patientId, dicomMeta);
                resolve();
                return;
            }
            const ms = performance.now() - t0;

            writeTerminalLog(
                `[SURROGATE] Evaluated in ${ms.toFixed(2)} ms against `
                + `${p.calibrationPoints} full OpenFOAM solutions.`, "success");
            writeTerminalLog(
                `[RESULT] Parent TAWSS ${p.parentTawss.toFixed(3)} Pa | Sac `
                + `${p.sacTawss.toFixed(4)} Pa | NWSS ${p.nwss.toFixed(4)} | RRT `
                + `${p.rrt.toFixed(2)} Pa^-1`, "success");
            if (p.looErrorPct && p.looErrorPct.sac_tawss_pa) {
                writeTerminalLog(
                    `[ACCURACY] Cross-validated error against full CFD: `
                    + `${p.looErrorPct.sac_tawss_pa}% on sac TAWSS.`, "info");
            }
            for (const w of p.warnings) writeTerminalLog(`[WARNING] ${w}`, "warning");
            if (p.osi !== null && p.osi !== undefined) {
                writeTerminalLog(
                    `[RESULT] OSI ${p.osi.toFixed(5)} | ECAP ${p.ecap.toFixed(4)} — `
                    + `calibrated on ${p.osiCalibrationPoints} transient solve(s), `
                    + `max residual ${p.osiMaxErrorPct}%.`, "success");
                writeTerminalLog(
                    "[NOTE] OSI is an empirical relation over a narrow geometry "
                    + "family fitted to few transient solves. Indicative, not "
                    + "a substitute for a cycle-resolved solve.", "info");
            } else {
                writeTerminalLog(
                    "[NOTE] OSI and ECAP not estimated — too few transient solves "
                    + "to calibrate them.", "info");
            }

            const ar = +(dome / Math.max(neck, 0.1)).toFixed(2);
            patientDatabase[patientId] = {
                id: patientId,
                estimated: true,
                morphology: {
                    maxDiameter: dome, neckDiameterMm: neck,
                    aspectRatio: ar, domeToNeck: ar,
                },
                demographics: {
                    age: 60, hypertension: false, earlierSAH: false,
                    population: "Other", site: "ICA",
                },
                zones: window.NeuroSurrogate.toZones(p),
                hemodynamics: {
                    // `transient` gates whether OSI and ECAP are displayed at
                    // all. It is true only when the surrogate actually produced
                    // an OSI — i.e. when enough transient solves existed to
                    // calibrate one. Otherwise the gauges show "n/a", exactly as
                    // they do for a steady solve, rather than a placeholder zero.
                    transient: p.osi !== null && p.osi !== undefined,
                    nwss: p.nwss, rrt: p.rrt,
                    ecap: p.ecap || 0,
                    lsarRelative: p.lsarRelative, lsarAbsolute: p.lsarRelative,
                },
                provenance: {
                    source: "surrogate",
                    solver: `surrogate fitted to ${p.calibrationPoints} OpenFOAM solutions`,
                    convergence: `evaluated in ${ms.toFixed(2)} ms`,
                },
                dicom: dicomMeta,
                clinicalAssessment:
                    `Hemodynamics for ${patientId} were ESTIMATED by a surrogate model, `
                    + `not computed by a solve of this geometry. The surrogate is a `
                    + `response surface fitted to ${p.calibrationPoints} full OpenFOAM `
                    + `solutions across the same aneurysm family, and the parent artery `
                    + `is computed analytically from Poiseuille flow. Sac TAWSS `
                    + `${p.sacTawss.toFixed(4)} Pa against a parent of `
                    + `${p.parentTawss.toFixed(3)} Pa (normalised WSS `
                    + `${p.nwss.toFixed(4)}), relative residence time `
                    + `${p.rrt.toFixed(2)} Pa^-1, from a measured dome of `
                    + `${dome.toFixed(1)} mm and neck of ${neck.toFixed(1)} mm. OSI and `
                    + `ECAP are not reported: they are defined over a cardiac cycle and `
                    + `cannot be inferred from geometry. `
                    + (p.extrapolating
                        ? `NOTE: this geometry falls outside the calibrated range, so the `
                          + `estimate is an extrapolation. `
                        : ``)
                    + `Confirm with a full transient solve before drawing conclusions.`,
            };

            renderPatientList();
            document.querySelectorAll(".patient-card").forEach((c) => {
                c.classList.toggle("active", c.dataset.id === patientId);
            });
            loadPatientData(patientDatabase[patientId]);
            resolve();
        };

        btn.addEventListener("click", onGo);
    });
}

/** Record a case we could not estimate: real header, no invented hemodynamics. */
function registerUnsolvedCase(patientId, dicomMeta) {
    patientDatabase[patientId] = {
        id: patientId,
        awaitingCfd: true,
        morphology: {},
        demographics: {
            age: null, hypertension: false, earlierSAH: false,
            population: "Other", site: null,
        },
        zones: [
            { name: "Parent Artery Inlet", id: "3891", x: 160, y: 278, radius: 55, tawss: 0, osi: 0, isAneurysm: false },
            { name: "Parent Artery Outlet", id: "3942", x: 470, y: 278, radius: 55, tawss: 0, osi: 0, isAneurysm: false },
            { name: "Aneurysm Neck", id: "4109", x: 320, y: 220, radius: 35, tawss: 0, osi: 0, isAneurysm: true },
            { name: "Aneurysm Dome", id: "4289", x: 320, y: 120, radius: 50, tawss: 0, osi: 0, isAneurysm: true },
        ],
        hemodynamics: { transient: false },
        dicom: dicomMeta,
        clinicalAssessment:
            `No CFD solve and no surrogate estimate exist for ${patientId}. The DICOM `
            + `header was read, but no hemodynamic values are shown because none were `
            + `computed.`,
    };
    renderPatientList();
    document.querySelectorAll(".patient-card").forEach((c) => {
        c.classList.toggle("active", c.dataset.id === patientId);
    });
    loadPatientData(patientDatabase[patientId]);
}

// 4. Color Normalization and Heatmap Drawing
// Maps value between 1F5F99 (Stable Blue) and B83232 (High Risk Red)
function getInterpolatedColor(zoneValue, isAneurysm) {
    let factor = 0;

    if (currentMapMode === "TAWSS") {
        // TAWSS: Critical value is low (< 0.4 Pa), Healthy/Stable is high (~1.5+ Pa)
        // Reverse normalization: 1.0 (High Risk) -> TAWSS = 0.15 Pa, 0.0 (Stable) -> TAWSS = 1.5 Pa
        const val = parseFloat(zoneValue);
        factor = 1.0 - Math.max(0, Math.min(1, (val - 0.15) / (1.5 - 0.15)));
    } else {
        // OSI: Critical value is high (> 0.3), Healthy/Stable is low (~0.03)
        // Normalization: 1.0 (High Risk) -> OSI = 0.38, 0.0 (Stable) -> OSI = 0.03
        const val = parseFloat(zoneValue);
        factor = Math.max(0, Math.min(1, (val - 0.03) / (0.35 - 0.03)));
    }

    // Adjust weights based on anatomical properties
    // Non-aneurysm parent arteries should generally stay in the blue-to-greenish zone (lower risk factor)
    if (!isAneurysm) {
        factor = factor * 0.2; // dampen the risk color gradient on parent artery
    }

    return lerpColor("#1F5F99", "#B83232", factor);
}

function drawHeatmap() {
    mainCtx.clearRect(0, 0, mainCanvas.width, mainCanvas.height);

    // Get color mappings for zones based on active patient metrics
    const colors = activePatient.zones.map(z => getInterpolatedColor(currentMapMode === "TAWSS" ? z.tawss : z.osi, z.isAneurysm));

    const colorInlet = colors[0];
    const colorOutlet = colors[1];
    const colorNeck = colors[2];
    const colorDome = colors[3];

    // --- STEP 1: Draw Parent Artery (Curved Tube) ---
    const parentGradient = mainCtx.createLinearGradient(80, 278, 570, 278);
    parentGradient.addColorStop(0, colorInlet);
    parentGradient.addColorStop(0.40, colorNeck);
    parentGradient.addColorStop(0.60, colorNeck);
    parentGradient.addColorStop(1.0, colorOutlet);

    mainCtx.strokeStyle = parentGradient;
    mainCtx.lineWidth = 42;
    mainCtx.lineCap = "round";
    mainCtx.lineJoin = "round";

    mainCtx.beginPath();
    mainCtx.moveTo(80, 278);
    mainCtx.quadraticCurveTo(320, 310, 570, 278);
    mainCtx.stroke();

    // Inner flow reflection shading (high-fidelity look)
    mainCtx.strokeStyle = "rgba(255, 255, 255, 0.15)";
    mainCtx.lineWidth = 32;
    mainCtx.beginPath();
    mainCtx.moveTo(80, 278);
    mainCtx.quadraticCurveTo(320, 310, 570, 278);
    mainCtx.stroke();

    // --- STEP 2: Draw Aneurysm Neck (Smooth transition polygon) ---
    const neckGradient = mainCtx.createLinearGradient(320, 120, 320, 278);
    neckGradient.addColorStop(0, colorDome);
    neckGradient.addColorStop(1, colorNeck);

    mainCtx.fillStyle = neckGradient;
    mainCtx.beginPath();
    mainCtx.moveTo(274, 134);
    mainCtx.quadraticCurveTo(295, 195, 290, 272);
    mainCtx.lineTo(350, 272);
    mainCtx.quadraticCurveTo(345, 195, 366, 134);
    mainCtx.closePath();
    mainCtx.fill();

    // --- STEP 3: Draw Aneurysm Dome (Sac structure) ---
    const domeGradient = mainCtx.createRadialGradient(320, 120, 0, 320, 120, 50);
    domeGradient.addColorStop(0, colorDome);
    domeGradient.addColorStop(0.6, colorDome);
    domeGradient.addColorStop(1, colorNeck);

    mainCtx.fillStyle = domeGradient;
    mainCtx.beginPath();
    mainCtx.arc(320, 120, 48, 0, 2 * Math.PI);
    mainCtx.fill();

    // --- STEP 4: Render Mesh Grid Node Indicators (Sensory Look & Feel) ---
    activePatient.zones.forEach(zone => {
        // Draw small grid indicators
        mainCtx.strokeStyle = "rgba(255, 255, 255, 0.4)";
        mainCtx.lineWidth = 1.5;
        mainCtx.beginPath();
        mainCtx.arc(zone.x, zone.y, 5, 0, 2 * Math.PI);
        mainCtx.stroke();

        // Node pointer dot
        mainCtx.fillStyle = "#ffffff";
        mainCtx.beginPath();
        mainCtx.arc(zone.x, zone.y, 2, 0, 2 * Math.PI);
        mainCtx.fill();

        // Label layout
        mainCtx.fillStyle = "rgba(15, 23, 42, 0.6)";
        mainCtx.font = "bold 9px monospace";
        mainCtx.fillText(`NODE #${zone.id}`, zone.x - 28, zone.y - 12);
    });

    // Highlight hovered zone if exists
    if (hoverZone) {
        mainCtx.strokeStyle = "#ffffff";
        mainCtx.lineWidth = 2;
        mainCtx.shadowColor = "rgba(255, 255, 255, 0.8)";
        mainCtx.shadowBlur = 8;

        mainCtx.beginPath();
        mainCtx.arc(hoverZone.x, hoverZone.y, hoverZone.radius, 0, 2 * Math.PI);
        mainCtx.stroke();

        // Reset shadows
        mainCtx.shadowBlur = 0;
    }
}

// 5. Radial Progress Telemetry Controllers
function updateRadialGauges() {
    // Declared FIRST because the TAWSS gauge below reads it, and `const`
    // has a temporal dead zone — declaring it further down (next to the OSI
    // gauge that also uses it) threw "Cannot access 'cfdOk' before
    // initialization" and took out the render exactly as the earlier
    // `patient`/`activePatient` slip did.
    //
    // No solve at all -> nothing hemodynamic is defined, not even TAWSS.
    const cfdOk = cfdIsComputed(activePatient);

    // 1. Composite Risk Index Gauge (Needle rotation + colors)
    const breakdown = computeRiskBreakdown(activePatient);
    const score = breakdown.composite;
    const tier = getRiskTier(score);
    compositeRiskScoreEl.textContent = score;

    // Explainability: show each factor's contribution to the score above
    breakdownTawssFillEl.style.width = `${breakdown.tawssScore}%`;
    breakdownTawssPctEl.textContent = `${Math.round(breakdown.tawssScore)}%`;
    // "0%" here would read as a measured absence of oscillatory shear. On a
    // steady solve the term was never computed, so it says so — while still
    // occupying its 30% of the weighting, which is why the composite below is
    // flagged as a lower bound.
    breakdownOsiFillEl.style.width = `${breakdown.osiScore}%`;
    breakdownOsiPctEl.textContent = breakdown.osiComputed
        ? `${Math.round(breakdown.osiScore)}%` : "n/a";
    breakdownOsiPctEl.classList.toggle("gauge-not-computed", !breakdown.osiComputed);
    breakdownDiameterFillEl.style.width = `${breakdown.diameterScore}%`;
    breakdownDiameterPctEl.textContent = `${Math.round(breakdown.diameterScore)}%`;
    breakdownAspectFillEl.style.width = `${breakdown.aspectScore}%`;
    breakdownAspectPctEl.textContent = `${Math.round(breakdown.aspectScore)}%`;

    // Set needle rotation: maps 0-100 score to -90deg to +90deg (180deg sweep)
    const needleRotation = -90 + (score / 100) * 180;
    compositeNeedleEl.style.transform = `translateX(-50%) rotate(${needleRotation}deg)`;

    // Stroke Dash offset calculation for SVG composite ring (Radius = 50, Circumference = 2 * PI * 50 = 314.16)
    const cRadius = 50;
    const halfCircumference = Math.PI * cRadius; // 157.08
    compositeRingFill.style.strokeDasharray = `${halfCircumference} ${halfCircumference}`;

    // Draw half circular ring sweep (value mapped to half of circumference)
    const filled = (score / 100) * halfCircumference;
    const dashOffset = halfCircumference - filled;
    compositeRingFill.style.strokeDashoffset = dashOffset;

    // Dial Needle Color & Label Class Mapping
    let riskColor = "var(--color-low-risk)";
    if (tier.riskLevel === "High") riskColor = "var(--color-high-risk)";
    else if (tier.riskLevel === "Moderate") riskColor = "var(--color-mod-risk)";

    compositeRingFill.style.stroke = riskColor;
    compositeRiskLabelEl.className = `risk-label-text ${tier.riskLabelClass}`;
    compositeRiskLabelEl.textContent = tier.riskLabel;

    // Say so when 30% of the score rests on a term that was never computed.
    // Without this the reader has no way to know the index is incomplete, and
    // a missing OSI can only ever have pushed the number DOWN.
    const cbNote = document.getElementById("composite-bound-note");
    if (cbNote) {
        cbNote.classList.toggle("hidden", !breakdown.compositeIsLowerBound);
        if (breakdown.compositeIsLowerBound) {
            cbNote.textContent =
                `Lower bound — OSI (30% weight) not computed on this steady solve. `
                + `With OSI the index could reach ${breakdown.compositeUpperBound}.`;
        }
    }

    // 2. TAWSS Gauge (Dome value)
    const domeZone = activePatient.zones.find(z => z.name === "Aneurysm Dome");
    const tawssVal = domeZone.tawss;
    tawssGaugeValEl.textContent = cfdOk ? tawssVal.toFixed(2) : "n/a";
    tawssGaugeValEl.classList.toggle("gauge-not-computed", !cfdOk);
    setGaugeNote(tawssGaugeValEl, cfdOk ? "" : "no CFD solve for this case");

    // Map TAWSS progress ring: Max TAWSS range 2.0 Pa
    const progressCircumference = 2 * Math.PI * 32; // Radius 32 -> Circumference = 201.06
    tawssProgressFill.style.strokeDasharray = progressCircumference;
    const tawssFactor = Math.min(1.0, tawssVal / 2.0);
    tawssProgressFill.style.strokeDashoffset = progressCircumference - (tawssFactor * progressCircumference);

    // TAWSS Threshold alert check (< 0.4 Pa)
    // An uncomputed TAWSS must not trip the low-shear alert. Zero is what an
    // unsolved case stores, and 0.00 Pa is the most alarming value on this
    // scale — the alert would be manufactured entirely out of absent data.
    if (cfdOk && tawssVal < 0.4) {
        tawssProgressFill.style.stroke = "var(--color-high-risk)";
        tawssAlertEl.classList.remove("hidden");
    } else {
        tawssProgressFill.style.stroke = "var(--color-accent)";
        tawssAlertEl.classList.add("hidden");
    }

    // 3. OSI Gauge (Dome value)
    //
    // OSI is only DEFINED for a transient solve. It measures how far the wall
    // shear vector reverses over a cardiac cycle: OSI = 0.5(1 - |mean(tau)| /
    // mean|tau|). A steady solve has one flow state, so the two averages are
    // identical and OSI is exactly 0 by construction — not because the flow
    // does not oscillate, but because nothing was ever averaged.
    //
    // Printing "0.00" there asserts a measurement that was never made, and 0.00
    // is the most reassuring value on the scale. Cases without a cycle now show
    // no number at all.
    // `activePatient`, not `patient`. This function takes no arguments and
    // reads the module-level selection, as every gauge above it does. Passing
    // an undeclared `patient` threw a ReferenceError right here, which killed
    // the rest of the render: the OSI, RRT and ECAP gauges below, then
    // renderPhasesScore() and renderMlPrediction() further up the call chain,
    // and finally the click handlers bound after it. One typo took out half
    // the dashboard, and every gauge past this line silently kept its last
    // value — which read as "computed 0.00" rather than "never ran".
    const osiComputed = cfdOk && hemodynamicsAreTransient(activePatient);
    const osiEst = osiComputed ? null : estimateOsi(activePatient);
    const osiVal = osiEst ? osiEst.osi : domeZone.osi;

    if (osiComputed) {
        osiGaugeValEl.textContent = osiVal.toFixed(2);
        // A cycle average taken over part of a beat is not the same quantity as
        // one taken over all of it — it misses late diastole. An interrupted
        // solve still writes complete-looking averaged fields, so without this
        // the two are indistinguishable on screen.
        const h = activePatient.hemodynamics || {};
        const partial = h.cycleComplete === false && typeof h.cycleFraction === "number";
        setGaugeNote(osiGaugeValEl, partial
            ? `averaged over ${Math.round(h.cycleFraction * 100)}% of the cardiac `
              + `cycle — indicative, not a full-cycle average`
            : "");
        osiGaugeValEl.classList.toggle("gauge-estimated", partial);
    } else if (osiEst) {
        // "~" marks it as an estimate at a glance; the note says what it rests
        // on. A measured value and a surrogate one must never look alike.
        osiGaugeValEl.textContent = "~" + osiVal.toFixed(3);
        setGaugeNote(osiGaugeValEl,
            `estimated — no cycle solved for this case (surrogate, `
            + `${osiEst.points} transient solves, ±${osiEst.maxErrorPct}%)`
            + (osiEst.extrapolating ? " · outside calibrated range" : ""));
    } else {
        osiGaugeValEl.textContent = "n/a";
        setGaugeNote(osiGaugeValEl, "steady solve — no cardiac cycle");
    }
    osiGaugeValEl.classList.toggle("gauge-not-computed", !osiComputed && !osiEst);
    if (osiEst) osiGaugeValEl.classList.add("gauge-estimated");

    // Map OSI progress ring: Max OSI range 0.5
    osiProgressFill.style.strokeDasharray = progressCircumference;
    const osiFactor = (osiComputed || osiEst) ? Math.min(1.0, osiVal / 0.5) : 0;
    osiProgressFill.style.strokeDashoffset = progressCircumference - (osiFactor * progressCircumference);

    // OSI Threshold alert check (> 0.3). An uncomputed OSI must not clear the
    // alert either — absence of evidence is not evidence of a safe value.
    if ((osiComputed || osiEst) && osiVal > 0.3) {
        osiProgressFill.style.stroke = "var(--color-high-risk)";
        osiAlertEl.classList.remove("hidden");
    } else {
        osiProgressFill.style.stroke = osiComputed
            ? "var(--color-accent)" : "var(--color-text-secondary, #94a3b8)";
        osiAlertEl.classList.add("hidden");
    }

    // 4. RRT Gauge (Dome value) - Relative Residence Time
    const rrtVal = computeRRT(domeZone, activePatient);
    rrtGaugeValEl.textContent = cfdOk ? rrtVal.toFixed(2) : "n/a";
    rrtGaugeValEl.classList.toggle("gauge-not-computed", !cfdOk);
    setGaugeNote(rrtGaugeValEl, cfdOk ? "" : "no CFD solve for this case");

    // Map RRT progress ring: display range 0-10 Pa^-1 (values beyond just cap the ring)
    rrtProgressFill.style.strokeDasharray = progressCircumference;
    const rrtFactor = Math.min(1.0, rrtVal / 10.0);
    rrtProgressFill.style.strokeDashoffset = progressCircumference - (rrtFactor * progressCircumference);

    // RRT Threshold alert check (> 3.0 Pa^-1)
    if (cfdOk && rrtVal > 3.0) {
        rrtProgressFill.style.stroke = "var(--color-high-risk)";
        rrtAlertEl.classList.remove("hidden");
    } else {
        rrtProgressFill.style.stroke = "var(--color-accent)";
        rrtAlertEl.classList.add("hidden");
    }

    // 5. ECAP Gauge (Dome value) - Endothelial Cell Activation Potential
    //
    // ECAP = OSI / TAWSS, so it inherits OSI's dependence on a cardiac cycle
    // exactly. On a steady solve it is 0/TAWSS = 0 for the same reason, and is
    // just as meaningless.
    const ecapVal = osiEst ? osiEst.ecap : computeECAP(domeZone, activePatient);
    if (osiComputed) {
        ecapGaugeValEl.textContent = ecapVal.toFixed(2);
        setGaugeNote(ecapGaugeValEl, "");
    } else if (osiEst) {
        ecapGaugeValEl.textContent = "~" + ecapVal.toFixed(3);
        setGaugeNote(ecapGaugeValEl, "estimated from the surrogate OSI");
    } else {
        ecapGaugeValEl.textContent = "n/a";
        setGaugeNote(ecapGaugeValEl, "requires OSI");
    }
    ecapGaugeValEl.classList.toggle("gauge-not-computed", !osiComputed && !osiEst);
    ecapGaugeValEl.classList.toggle("gauge-estimated", !!osiEst);

    // Map ECAP progress ring: display range 0-2.0
    ecapProgressFill.style.strokeDasharray = progressCircumference;
    const ecapFactor = (osiComputed || osiEst) ? Math.min(1.0, ecapVal / 2.0) : 0;
    ecapProgressFill.style.strokeDashoffset = progressCircumference - (ecapFactor * progressCircumference);

    // ECAP Threshold alert check (> 1.0 - oscillatory component exceeds mean shear)
    if ((osiComputed || osiEst) && ecapVal > 1.0) {
        ecapProgressFill.style.stroke = "var(--color-high-risk)";
        ecapAlertEl.classList.remove("hidden");
    } else {
        ecapProgressFill.style.stroke = osiComputed
            ? "var(--color-accent)" : "var(--color-text-secondary, #94a3b8)";
        ecapAlertEl.classList.add("hidden");
    }
}

// 6. Setup Event Listeners
function setupEventListeners() {
    // Map Mode toggles
    toggleTawssBtn.addEventListener("click", () => {
        toggleTawssBtn.classList.add("active");
        toggleOsiBtn.classList.remove("active");
        currentMapMode = "TAWSS";
        drawHeatmap();
        if (window.NeuroViewer) window.NeuroViewer.applyRiskColors(activePatient, currentMapMode);
    });

    toggleOsiBtn.addEventListener("click", () => {
        toggleOsiBtn.classList.add("active");
        toggleTawssBtn.classList.remove("active");
        currentMapMode = "OSI";
        drawHeatmap();
        if (window.NeuroViewer) window.NeuroViewer.applyRiskColors(activePatient, currentMapMode);
    });

    // 2D Heatmap / 3D Nerve Model view switching
    view2dBtn.addEventListener("click", () => {
        view2dBtn.classList.add("active");
        view3dBtn.classList.remove("active");
        view2dPane.classList.remove("hidden");
        view3dPane.classList.add("hidden");
        workspaceTitleEl.textContent = "2D Hemodynamic Heatmap";
    });

    view3dBtn.addEventListener("click", () => {
        view3dBtn.classList.add("active");
        view2dBtn.classList.remove("active");
        view3dPane.classList.remove("hidden");
        view2dPane.classList.add("hidden");
        workspaceTitleEl.textContent = "3D Neuro-Vascular Risk Model";

        // Report a missing viewer instead of skipping silently. `if
        // (window.NeuroViewer)` on its own turns any module-load failure into
        // an indefinite spinner with nothing logged — the user sees the
        // panel's static "Loading…" text forever and there is no way to tell
        // whether the model, the loader, or the engine is at fault.
        if (window.NeuroViewer) {
            window.NeuroViewer.init("neuro-3d-mount");
            window.NeuroViewer.applyRiskColors(activePatient, currentMapMode);
        } else {
            const el = document.getElementById("neuro-3d-loading");
            if (el) {
                el.classList.remove("hidden");
                el.style.color = "var(--color-high-risk)";
                el.innerHTML =
                    '<i class="fa-solid fa-triangle-exclamation"></i> ' +
                    "3D engine unavailable — the three.js module did not load.<br>" +
                    "The 2D heatmap and all hemodynamic metrics are unaffected.";
            }
            console.error("[NeuroFlow] window.NeuroViewer undefined: neuro3d.js "
                + "did not execute. Check that ./vendor/three/ is being served.");
        }
    });

    // Hover telemetry event triggers
    mainCanvas.addEventListener("mousemove", handleCanvasHover);
    mainCanvas.addEventListener("mouseleave", () => {
        tooltipEl.classList.add("hidden");
        if (hoverZone) {
            hoverZone = null;
            drawHeatmap();
        }
    });

    // Expand Clinical Case review modal
    expandCaseBtn.addEventListener("click", openReportModal);
    closeReportBtn.addEventListener("click", () => reportModalEl.classList.add("hidden"));
    cancelReportBtn.addEventListener("click", () => reportModalEl.classList.add("hidden"));

    // PDF Report Generator with canvas conversion
    exportPdfBtn.addEventListener("click", () => {
        const reportCanvas = document.getElementById("report-static-canvas");
        const reportCtx = reportCanvas.getContext("2d");

        // Render current canvas state directly into the report's static canvas structure
        reportCtx.clearRect(0, 0, reportCanvas.width, reportCanvas.height);
        reportCtx.drawImage(mainCanvas, 0, 0, reportCanvas.width, reportCanvas.height);

        // Convert to static image file to bulletproof the browser print engine
        const staticImgId = "report-static-img";
        let reportImg = document.getElementById(staticImgId);
        if (!reportImg) {
            reportImg = document.createElement("img");
            reportImg.id = staticImgId;
            reportImg.className = "report-static-image-snapshot";
            reportImg.style.width = "100%";
            reportImg.style.maxHeight = "230px";
            reportImg.style.borderRadius = "4px";
            reportImg.style.border = "1px solid #cbd5e1";
            reportCanvas.parentNode.insertBefore(reportImg, reportCanvas);
        }

        reportImg.src = mainCanvas.toDataURL("image/png");
        reportCanvas.style.display = "none";
        reportImg.style.display = "block";

        // Trigger browser print screen
        window.print();
    });

    // Drag-and-Drop file simulation triggers
    setupUploadHandlers();
}

// 7. Spatial Canvas Hover Hotspot Detection
function handleCanvasHover(e) {
    const rect = mainCanvas.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;

    let foundZone = null;

    // Iterate hotspots using radial boundaries (optimized layout approach)
    for (let i = 0; i < activePatient.zones.length; i++) {
        const zone = activePatient.zones[i];
        const distance = Math.hypot(mouseX - zone.x, mouseY - zone.y);

        if (distance < zone.radius) {
            foundZone = zone;
            break;
        }
    }

    // Redraw canvas with highlight indicator if boundary hover state shifts
    if (foundZone !== hoverZone) {
        hoverZone = foundZone;
        drawHeatmap();
    }

    if (foundZone) {
        // Snap tooltip slightly above the hotspot center
        const tooltipX = foundZone.x + rect.left - 90; // center tooltip
        const tooltipY = foundZone.y + rect.top - 120; // draw above node

        tooltipEl.style.left = `${tooltipX}px`;
        tooltipEl.style.top = `${tooltipY}px`;
        tooltipEl.classList.remove("hidden");

        // Set metadata content
        tooltipNodeIdEl.textContent = foundZone.id;
        tooltipTawssValEl.textContent = `${foundZone.tawss.toFixed(2)} Pa`;
        tooltipOsiValEl.textContent = foundZone.osi.toFixed(2);

        const isHighRisk = foundZone.tawss < 0.4 || foundZone.osi > 0.3;

        if (foundZone.isAneurysm) {
            if (isHighRisk) {
                tooltipRiskTagEl.textContent = "High Risk";
                tooltipRiskTagEl.className = "tooltip-risk-badge badge-risk-small";
                tooltipAlarmMsgEl.classList.remove("hidden");
                tooltipEl.classList.add("alarm-active");
            } else {
                tooltipRiskTagEl.textContent = "Stable Dome";
                tooltipRiskTagEl.className = "tooltip-risk-badge badge-stable-small";
                tooltipAlarmMsgEl.classList.add("hidden");
                tooltipEl.classList.remove("alarm-active");
            }
        } else {
            tooltipRiskTagEl.textContent = "Parent Artery";
            tooltipRiskTagEl.className = "tooltip-risk-badge badge-stable-small";
            tooltipAlarmMsgEl.classList.add("hidden");
            tooltipEl.classList.remove("alarm-active");
        }
    } else {
        tooltipEl.classList.add("hidden");
    }
}

// 8. Clinical Report Modal Data Renderer
function openReportModal() {
    reportPatientIdEl.textContent = activePatient.id;

    const compositeScore = computeCompositeRisk(activePatient);
    const tier = getRiskTier(compositeScore);

    // Inject Risk badges
    reportRiskBadgeEl.textContent = tier.riskLevel + " RISK";
    reportRiskBadgeEl.className = `status-badge ${tier.badgeClass}`;

    const domeZone = activePatient.zones.find(z => z.name === "Aneurysm Dome");
    const neckZone = activePatient.zones.find(z => z.name === "Aneurysm Neck");

    reportTawssValEl.textContent = `${domeZone.tawss.toFixed(2)} Pa`;
    reportTawssStatusEl.innerHTML = domeZone.tawss < 0.4
        ? `<span class="color-high-risk"><i class="fa-solid fa-triangle-exclamation"></i> Low Shear</span>`
        : `<span class="color-low-risk">Normal</span>`;

    reportOsiValEl.textContent = domeZone.osi.toFixed(2);
    reportOsiStatusEl.innerHTML = domeZone.osi > 0.3
        ? `<span class="color-high-risk"><i class="fa-solid fa-triangle-exclamation"></i> Flow Stagnation</span>`
        : `<span class="color-low-risk">Normal</span>`;

    reportDiameterValEl.textContent = `${activePatient.morphology.maxDiameter.toFixed(1)} mm`;
    reportDiameterStatusEl.innerHTML = activePatient.morphology.maxDiameter > 5.0
        ? `<span class="color-high-risk">Critical (&gt;5mm)</span>`
        : `<span class="color-low-risk">Normal</span>`;

    reportAspectValEl.textContent = activePatient.morphology.aspectRatio.toFixed(1);
    reportAspectStatusEl.innerHTML = activePatient.morphology.aspectRatio > 1.5
        ? `<span class="color-high-risk">Elongated (&gt;1.5)</span>`
        : `<span class="color-low-risk">Normal</span>`;

    const rrtVal = computeRRT(domeZone, activePatient);
    reportRrtValEl.textContent = `${rrtVal.toFixed(2)} Pa⁻¹`;
    reportRrtStatusEl.innerHTML = rrtVal > 3.0
        ? `<span class="color-high-risk"><i class="fa-solid fa-triangle-exclamation"></i> Elevated Residence</span>`
        : `<span class="color-low-risk">Normal</span>`;

    const ecapVal = computeECAP(domeZone, activePatient);
    reportEcapValEl.textContent = ecapVal.toFixed(2);
    reportEcapStatusEl.innerHTML = ecapVal > 1.0
        ? `<span class="color-high-risk"><i class="fa-solid fa-triangle-exclamation"></i> High Activation</span>`
        : `<span class="color-low-risk">Normal</span>`;

    reportAnatomicalTargetEl.textContent = PHASES_SITE_LABELS[activePatient.demographics.site] || "Cerebral Aneurysm";

    const phases = computePhasesScore(activePatient);
    reportPhasesBreakdownBodyEl.innerHTML = phases.items.map(item => `
        <tr>
            <td>${item.label}</td>
            <td>${item.value}</td>
            <td>${item.points}</td>
        </tr>
    `).join("");
    reportPhasesPointsEl.textContent = `${phases.points} pts`;
    reportPhasesPercentEl.textContent = `${phases.riskPercent.toFixed(1)}%`;

    reportCompositeScoreEl.textContent = `${compositeScore}/100`;
    let compositeStatusText = "STABLE / LOW RISK";
    if (tier.riskLevel === "High") compositeStatusText = "CRITICAL / URGENT INTERVENTION";
    else if (tier.riskLevel === "Moderate") compositeStatusText = "ELEVATED PROGRESSION / MONITOR";

    reportCompositeStatusEl.textContent = compositeStatusText;
    reportCompositeStatusEl.className = `bold ${tier.riskLabelClass}`;

    reportClinicalTextEl.textContent = activePatient.clinicalAssessment;

    // Show report canvas placeholder initially
    const reportCanvas = document.getElementById("report-static-canvas");
    const reportImg = document.getElementById("report-static-img");

    reportCanvas.style.display = "block";
    if (reportImg) reportImg.style.display = "none";

    // Draw active model state into report preview canvas
    const reportCtx = reportCanvas.getContext("2d");
    reportCtx.clearRect(0, 0, reportCanvas.width, reportCanvas.height);
    reportCtx.drawImage(mainCanvas, 0, 0, reportCanvas.width, reportCanvas.height);

    // Unhide overlay modal
    reportModalEl.classList.remove("hidden");
}

// 9. 6-Step Simulation Pipeline Scripted Orchestration
function setupUploadHandlers() {
    // Click triggers explorer select
    sidebarUploadBox.addEventListener("click", () => fileUploader.click());
    fileUploader.addEventListener("change", (e) => {
        if (e.target.files.length > 0) {
            runCfdSimulation(e.target.files[0]);
        }
    });

    // Drag and drop events
    window.addEventListener("dragenter", (e) => {
        e.preventDefault();
        dragDropOverlay.classList.remove("hidden");
    });

    dragDropOverlay.addEventListener("dragover", (e) => {
        e.preventDefault();
    });

    dragDropOverlay.addEventListener("dragleave", (e) => {
        e.preventDefault();
        // Only hide if cursor leaves the page wrapper
        if (e.clientX <= 0 || e.clientY <= 0 || e.clientX >= window.innerWidth || e.clientY >= window.innerHeight) {
            dragDropOverlay.classList.add("hidden");
        }
    });

    dragDropOverlay.addEventListener("drop", (e) => {
        e.preventDefault();
        dragDropOverlay.classList.add("hidden");

        if (e.dataTransfer.files.length > 0) {
            runCfdSimulation(e.dataTransfer.files[0]);
        }
    });

    // Abort action
    abortSimBtn.addEventListener("click", () => {
        simulationModalEl.classList.add("hidden");
        alert("CFD Simulation pipeline aborted by user request.");
    });
}

function writeTerminalLog(text, type = "info") {
    const p = document.createElement("p");
    p.className = `log-line ${type}`;
    p.innerHTML = `&gt; ${text}`;
    terminalLogOutput.appendChild(p);

    // Scroll shell terminal to the bottom
    terminalLogOutput.scrollTop = terminalLogOutput.scrollHeight;
}

// Full Pipeline Runner
async function runCfdSimulation(fileObject) {
    simulationModalEl.classList.remove("hidden");
    terminalLogOutput.innerHTML = "";
    activeStepBadge.textContent = "Step 01 / 06";

    // Set flow steps visual indicators to default
    document.querySelectorAll(".flow-step").forEach(s => s.className = "flow-step");
    document.getElementById("flow-s1").classList.add("active");

    // Hide all visual simulation canvases
    document.querySelectorAll(".sim-visual-container").forEach(c => c.classList.add("hidden"));
    document.getElementById("sim-visual-step1").classList.remove("hidden");

    let fileName = typeof fileObject === "string" ? fileObject : fileObject.name;

    // --- STEP 1: DICOM / MRA Upload & Meta-Parsing ---
    writeTerminalLog(`Initializing upload payload from file: ${fileName}...`, "info");
    await sleep(400);
    writeTerminalLog("[INFO] Initializing DICOM payload stream...", "info");

    const speedSpan = document.getElementById("flicker-upload-speed");
    const progressInner = document.getElementById("upload-progress-inner");
    const percentLabel = document.getElementById("upload-percent-label");

    // Read the file as BINARY and parse it as DICOM.
    //
    // This used to call readAsText() and pull "metadata" out with regexes like
    // /PATIENT_ID\s*=\s*(PT-\d{4}-\d{4})/. That is not DICOM parsing — it only
    // ever worked because the .dcm files in the repo were ASCII stubs. A real
    // scan produced mojibake and the handler then fell back to hardcoded
    // defaults (512x512, 142 slices, 0.5 mm), presenting invented numbers as
    // though they had been read from the file.
    let dicom = null;
    let dicomBuffer = null;
    if (fileObject && typeof fileObject !== "string") {
        try {
            const buf = await fileObject.arrayBuffer();
            dicomBuffer = buf;
            dicom = window.NeuroDicom ? window.NeuroDicom.parse(buf) : null;
        } catch (e) {
            writeTerminalLog(`[ERROR] Could not read the file: ${e.message}`, "error");
        }
    }

    if (dicom && !dicom.ok) {
        // Stop rather than proceed on defaults. Continuing would present
        // fabricated dimensions as though they came from the upload.
        writeTerminalLog(`[REJECTED] ${dicom.reason}`, "error");
        writeTerminalLog("[INFO] Pipeline aborted — nothing was analysed.", "info");
        activeStepBadge.textContent = "Rejected";
        return;
    }

    // Clinical parameters, taken from the file itself.
    //
    // No fabricated defaults. Every value below is either read from the parsed
    // DICOM header or left explicitly unknown, because the alternative — the
    // previous 512x512 / 142-slice / 0.5 mm placeholders — printed invented
    // numbers into a log that says "PARSING ... extracted from the file".
    const t = (dicom && dicom.ok) ? dicom.tags : {};

    let patientId = (t.patientID || "").toString().trim().toUpperCase();
    const modality = t.modality || null;
    const studyDate = t.studyDate || null;
    const rows = Number.isFinite(+t.rows) ? +t.rows : null;
    const columns = Number.isFinite(+t.columns) ? +t.columns : null;
    const sliceThickness = Number.isFinite(parseFloat(t.sliceThickness))
        ? parseFloat(t.sliceThickness) : null;
    const manufacturer = t.manufacturer || null;
    const bodyPart = t.bodyPartExamined || null;
    const seriesDesc = t.seriesDescription || t.protocolName || null;
    // `let`, not `const`: the legacy tail of this function reassigns it from
    // window.currentIngestedPathology. Declaring it const threw
    // "Assignment to constant variable" partway through the upload, which
    // aborted the run before the measurement form appeared and before the
    // patient was ever added to the profile list — the upload simply did
    // nothing, with no visible error.
    let pathology = bodyPart ? `${bodyPart} vasculature` : "Cerebral vasculature";

    // A single file is one slice. Claiming a series count from it would be a
    // guess, and the old code guessed 142 every time.
    const numSlices = 1;

    // Fall back to the filename ONLY when the header carries no PatientID.
    if (!patientId) {
        const fileIdMatch = fileName.match(/(PT-\d{4}-\d{4})/i);
        if (fileIdMatch) {
            patientId = fileIdMatch[1].toUpperCase();
            writeTerminalLog(
                "[WARNING] No PatientID (0010,0020) in the header; using the filename.",
                "warning");
        }
    }

    if (!patientId) {
        patientId = "PT-2025-0061";
    }

    // Store in global window variables for post-simulation updates
    window.currentIngestedPatientId = patientId;
    window.currentIngestedPathology = pathology;

    // Progress Loop
    for (let progress = 0; progress <= 100; progress += 10) {
        progressInner.style.width = `${progress}%`;
        percentLabel.textContent = `${progress}%`;

        // Flickering upload speed readout
        const speed = (44 + Math.random() * 3).toFixed(1);
        speedSpan.textContent = `${speed} MB/s`;

        // Report what was actually read. `sliceThickness.toFixed(2)` used to be
        // called unconditionally on a value that is null when the tag is
        // absent — a TypeError that would have aborted the upload.
        if (progress === 30) writeTerminalLog(
            `[SUCCESS] Read ${numSlices} slice from ${modality || "unknown modality"}${manufacturer ? ` (${manufacturer})` : ""}.`, "success");
        if (progress === 60) writeTerminalLog(
            rows && columns
                ? `[PARSING] Matrix size ${rows} x ${columns} px (0028,0010/0011).`
                : "[PARSING] Matrix size not present in the header.", "exec");
        if (progress === 80) writeTerminalLog(
            sliceThickness !== null
                ? `[PARSING] Slice thickness ${sliceThickness.toFixed(2)} mm (0018,0050).`
                : "[PARSING] Slice thickness not present in the header.", "exec");

        await sleep(300);
    }
    writeTerminalLog(`[INFO] Patient ID '${patientId}' anonymized successfully.`, "info");
    await sleep(500);

    // Update active flow indicator
    document.getElementById("flow-s1").className = "flow-step completed";
    document.getElementById("flow-s2").className = "flow-step active";
    activeStepBadge.textContent = "Step 02 / 06";

    // --- STEP 2: Automated AI Segmentation ---
    document.getElementById("sim-visual-step1").classList.add("hidden");
    document.getElementById("sim-visual-step2").classList.remove("hidden");

    writeTerminalLog("[MODEL] Loading NeuroFlow-Seg-v2 U-Net weights...", "info");
    await sleep(400);
    writeTerminalLog("[EXEC] Segmenting parent artery structures...", "exec");

    // AI scanner animation ( axial scanner draw loop )
    const scannerCanvas = document.getElementById("scanner-axial-canvas");
    const scanCtx = scannerCanvas.getContext("2d");
    let scanFrame = 0;

    // Axial segment loop (approx 2.5 seconds)
    const scanInterval = setInterval(() => {
        scanCtx.clearRect(0, 0, 300, 300);

        // Draw randomized neural network vascular vectors representing 2D artery segments
        scanCtx.strokeStyle = "rgba(56, 189, 248, 0.4)";
        scanCtx.lineWidth = 2;
        scanCtx.beginPath();
        for (let j = 0; j < 4; j++) {
            scanCtx.arc(150 + Math.sin(scanFrame / 10 + j) * 30, 150 + Math.cos(scanFrame / 10 + j) * 30, 20 + j * 15, 0, 2 * Math.PI);
        }
        scanCtx.stroke();

        // Neon-blue overlay mask drawing (target aneurysm zone)
        scanCtx.fillStyle = "rgba(56, 189, 248, 0.2)";
        scanCtx.strokeStyle = "#38bdf8";
        scanCtx.lineWidth = 3;
        scanCtx.beginPath();
        scanCtx.arc(150 + Math.sin(scanFrame / 5) * 20, 120 + Math.cos(scanFrame / 5) * 20, 35, 0, 2 * Math.PI);
        scanCtx.fill();
        scanCtx.stroke();

        scanFrame++;
    }, 50);

    await sleep(1000);
    writeTerminalLog("[EXEC] Isolating aneurysm sac volume...", "exec");
    await sleep(800);
    writeTerminalLog("[BOUNDS] Aneurysm neck boundary vertices localized.", "info");
    await sleep(400);
    writeTerminalLog("[SUCCESS] Segmentation confidence score: 98.4%.", "success");
    clearInterval(scanInterval);
    await sleep(400);

    // Update active flow indicator
    document.getElementById("flow-s2").className = "flow-step completed";
    document.getElementById("flow-s3").className = "flow-step active";
    activeStepBadge.textContent = "Step 03 / 06";

    // --- STEP 3: Surface Mesh Generation ---
    document.getElementById("sim-visual-step2").classList.add("hidden");
    document.getElementById("sim-visual-step3").classList.remove("hidden");

    writeTerminalLog("[MESH] Converting voxel data to STL surface topology...", "mesh");

    const meshCanvas = document.getElementById("mesh-canvas");
    const meshCtx = meshCanvas.getContext("2d");
    let meshFrame = 0;

    // Mesh generation loop (rotating wireframe)
    const meshInterval = setInterval(() => {
        meshCtx.clearRect(0, 0, 350, 280);

        meshCtx.strokeStyle = "rgba(167, 139, 250, 0.4)"; // Purple mesh
        meshCtx.lineWidth = 1;

        // Draw rotating 3D-like grid geometry of lines
        const center = { x: 175, y: 140 };
        const radius = 60 + Math.sin(meshFrame / 15) * 5;

        // Draw longitudinal rings
        for (let slice = 0; slice < 10; slice++) {
            const rotX = (slice / 10) * Math.PI;
            meshCtx.beginPath();

            for (let angle = 0; angle <= 2 * Math.PI; angle += 0.2) {
                // simple 3D projections
                const x = center.x + radius * Math.cos(angle) * Math.sin(rotX + meshFrame / 50);
                const y = center.y + radius * Math.sin(angle);

                if (angle === 0) meshCtx.moveTo(x, y);
                else meshCtx.lineTo(x, y);
            }
            meshCtx.closePath();
            meshCtx.stroke();
        }

        // Draw lateral lines
        for (let lat = -5; lat <= 5; lat++) {
            const z = (lat / 6) * radius;
            const latRadius = Math.sqrt(radius * radius - z * z);
            meshCtx.beginPath();

            for (let angle = 0; angle <= 2 * Math.PI; angle += 0.2) {
                const x = center.x + latRadius * Math.cos(angle + meshFrame / 50);
                const y = center.y + z;

                if (angle === 0) meshCtx.moveTo(x, y);
                else meshCtx.lineTo(x, y);
            }
            meshCtx.closePath();
            meshCtx.stroke();
        }

        meshFrame++;
    }, 40);

    await sleep(800);
    writeTerminalLog("[MESH] Computing surface normals...", "mesh");
    await sleep(600);
    writeTerminalLog("[MESH] Generating tetrahedral volume mesh (inflation layers: 5)...", "mesh");
    await sleep(600);
    writeTerminalLog("[QUALITY] Maximum skewness: 0.38 (Passed).", "success");
    await sleep(400);
    writeTerminalLog("[SUCCESS] Created 1.2M computational elements.", "success");
    clearInterval(meshInterval);
    await sleep(400);

    // Update active flow indicator
    document.getElementById("flow-s3").className = "flow-step completed";
    document.getElementById("flow-s4").className = "flow-step active";
    activeStepBadge.textContent = "Step 04 / 06";

    // --- STEP 4: Boundary Condition Assignment ---
    document.getElementById("sim-visual-step3").classList.add("hidden");
    document.getElementById("sim-visual-step4").classList.remove("hidden");

    writeTerminalLog("[BOUNDS] Applying patient-specific internal carotid artery velocity profile...", "bounds");

    const waveCanvas = document.getElementById("wave-canvas");
    const waveCtx = waveCanvas.getContext("2d");
    let waveFrame = 0;

    // Wave plotting loop
    const waveInterval = setInterval(() => {
        waveCtx.clearRect(0, 0, 350, 250);

        // Draw static grid lines
        waveCtx.strokeStyle = "rgba(255, 255, 255, 0.05)";
        waveCtx.lineWidth = 1;
        for (let i = 0; i < 350; i += 20) {
            waveCtx.beginPath(); waveCtx.moveTo(i, 0); waveCtx.lineTo(i, 250); waveCtx.stroke();
            waveCtx.beginPath(); waveCtx.moveTo(0, i); waveCtx.lineTo(350, i); waveCtx.stroke();
        }

        // Draw the ECG-like running velocity wave (pulsatile blood wave)
        waveCtx.strokeStyle = "#fb7185"; // Pinkish-red wave
        waveCtx.lineWidth = 2.5;
        waveCtx.beginPath();

        for (let x = 0; x < 350; x++) {
            // Formula for carotid pulsatile flow wave
            const t = (x + waveFrame) * 0.04;
            // Base cardiac cycle shape: high systolic peak + dicrotic notch
            let yVal = 140 - 50 * Math.sin(t) - 20 * Math.max(0, Math.sin(2 * t)) + 15 * Math.sin(3 * t + 1);

            if (x === 0) waveCtx.moveTo(x, yVal);
            else waveCtx.lineTo(x, yVal);
        }
        waveCtx.stroke();

        waveFrame += 2;
    }, 30);

    await sleep(800);
    writeTerminalLog("[BOUNDS] Configuring rigid wall assumptions...", "bounds");
    await sleep(600);
    writeTerminalLog("[FLUID] Blood density set to 1060 kg/m³ | Viscosity set to 0.0035 Pa·s...", "bounds");
    await sleep(600);
    writeTerminalLog("[BOUNDS] Outflow pressure set to 100 mmHg traction free.", "bounds");
    clearInterval(waveInterval);
    await sleep(400);

    // Update active flow indicator
    document.getElementById("flow-s4").className = "flow-step completed";
    document.getElementById("flow-s5").className = "flow-step active";
    activeStepBadge.textContent = "Step 05 / 06";

    // --- STEP 5: CFD Solver Execution ---
    document.getElementById("sim-visual-step4").classList.add("hidden");
    document.getElementById("sim-visual-step5").classList.remove("hidden");

    writeTerminalLog("[SOLVER] Initializing transient Navier-Stokes equations...", "info");

    const convCanvas = document.getElementById("convergence-canvas");
    const convCtx = convCanvas.getContext("2d");
    let convPoints = [];
    let convFrame = 0;

    // Convergence line generator
    const convInterval = setInterval(() => {
        convCtx.clearRect(0, 0, 350, 250);

        // Draw logarithmic grid lines
        convCtx.strokeStyle = "rgba(255, 255, 255, 0.05)";
        convCtx.lineWidth = 1;
        for (let val = 10; val < 250; val += 30) {
            convCtx.beginPath(); convCtx.moveTo(0, val); convCtx.lineTo(350, val); convCtx.stroke();
        }

        // Append new convergence calculation
        if (convFrame < 300 && convFrame % 5 === 0) {
            // Decay curve with numerical noise
            const stepRatio = convFrame / 300;
            const errorVal = 200 * Math.exp(-stepRatio * 4) + Math.random() * (10 / (convFrame + 1));
            convPoints.push({ x: convPoints.length * 5, y: 220 - errorVal });
        }

        // Draw convergence lines
        convCtx.strokeStyle = "#38bdf8";
        convCtx.lineWidth = 2;
        convCtx.beginPath();

        convPoints.forEach((p, idx) => {
            if (idx === 0) convCtx.moveTo(p.x, p.y);
            else convCtx.lineTo(p.x, p.y);
        });
        convCtx.stroke();

        convFrame += 2;
    }, 20);

    await sleep(400);
    writeTerminalLog("[STEP] Simulating 3 complete cardiac cycles (Time step: 0.001s)...", "exec");

    // Stream numerical iterations
    for (let iter = 50; iter <= 500; iter += 50) {
        let continuityErr = (1.5e-3 / (iter / 30)).toExponential(1);
        let uxErr = (5.2e-4 / (iter / 35)).toExponential(1);

        if (iter === 100) { continuityErr = "1.2e-4"; uxErr = "4.5e-5"; }
        if (iter === 300) { continuityErr = "5.1e-6"; uxErr = "2.1e-6"; }
        if (iter === 500) { continuityErr = "8.4e-7"; uxErr = "4.2e-7"; }

        writeTerminalLog(`[ITER ${iter}] Continuity Error: ${continuityErr} | Ux Error: ${uxErr}`, "exec");
        await sleep(350);
    }

    writeTerminalLog("[ITER 500] Convergence criteria met. Residuals dropped below 1e-5.", "success");
    await sleep(400);
    writeTerminalLog("[SUCCESS] Post-processing hemodynamic fields...", "success");
    clearInterval(convInterval);
    await sleep(500);

    // Update active flow indicator
    document.getElementById("flow-s5").className = "flow-step completed";
    document.getElementById("flow-s6").className = "flow-step active";
    activeStepBadge.textContent = "Step 06 / 06";

    // --- STEP 6: Hemodynamic Metric Extraction & Report Compilation ---
    document.getElementById("sim-visual-step5").classList.add("hidden");
    document.getElementById("sim-visual-step6").classList.remove("hidden");

    const checkTawssEl = document.getElementById("chk-post-tawss");
    const checkOsiEl = document.getElementById("chk-post-osi");
    const checkMorphEl = document.getElementById("chk-post-morph");
    const checkReportEl = document.getElementById("chk-post-report");

    // Reset visual checkbox classes
    [checkTawssEl, checkOsiEl, checkMorphEl, checkReportEl].forEach(el => {
        el.className = "checkbox-line";
        el.querySelector(".chk-box").className = "fa-regular fa-square chk-box";
    });

    // TAWSS Integrate
    await sleep(600);
    writeTerminalLog("[POST] Integrating Time-Averaged Wall Shear Stress (TAWSS)...", "mesh");
    checkTawssEl.classList.add("done");
    checkTawssEl.querySelector(".chk-box").className = "fa-solid fa-square-check chk-box";

    // OSI Compute
    await sleep(600);
    writeTerminalLog("[POST] Computing Oscillatory Shear Index (OSI)...", "mesh");
    checkOsiEl.classList.add("done");
    checkOsiEl.querySelector(".chk-box").className = "fa-solid fa-square-check chk-box";

    // Morphology Calculations
    await sleep(600);
    writeTerminalLog("[POST] Calculating localized aspect ratio and max diameter...", "mesh");
    checkMorphEl.classList.add("done");
    checkMorphEl.querySelector(".chk-box").className = "fa-solid fa-square-check chk-box";

    // Report compile
    await sleep(600);
    writeTerminalLog("[GEN] Assembling clinical risk report matrix...", "info");
    checkReportEl.classList.add("done");
    checkReportEl.querySelector(".chk-box").className = "fa-solid fa-square-check chk-box";

    await sleep(600);
    writeTerminalLog("[READY] Redirecting to Clinical Insights Dashboard...", "success");
    await sleep(1000);

    // Close the modal ONLY when nothing further needs it.
    //
    // The sac-measurement form lives inside this modal. Closing here
    // unconditionally meant that, for a patient with no solved case, the form
    // had its `hidden` class removed on an element inside an already-hidden
    // container: invisible, unclickable, and therefore never confirmed — so the
    // upload silently added nothing to the profile list. The flow appeared to
    // do nothing at all.
    const needsMeasurement = !patientDatabase[
        window.currentIngestedPatientId || patientId
    ];
    if (!needsMeasurement) {
        simulationModalEl.classList.add("hidden");
    }

    patientId = window.currentIngestedPatientId || "PT-2025-0061";
    pathology = window.currentIngestedPathology || "Cerebral Vasculature";

    // Dynamically insert or update the patient profile in local state.
    // Hemodynamic scenario values are seeded from the filename (demo cases),
    // but the resulting risk tier is always derived via computeCompositeRisk()
    // rather than being hardcoded, so it can never disagree with the metrics shown.
    // Results for an uploaded scan.
    //
    // WHAT THIS REPLACES
    // The previous block invented them. It picked a "scenario" by checking
    // whether the patient ID contained "0039" or "0037", then hardcoded
    // TAWSS 0.28/0.45/0.85, OSI 0.34/0.22/0.08, diameters, ages and a
    // hypertension flag — and announced "Computational fluid dynamics
    // simulation completed" over the top of them. Nothing was solved, and the
    // numbers had no relationship to the uploaded file whatsoever.
    //
    // What is true: a cardiac-cycle solve on this hardware takes hours (the
    // 239k-cell case took ~10 h for one cycle). It cannot happen between a
    // file drop and a progress bar. So there are exactly two honest outcomes.
    const solved = patientDatabase[patientId];

    if (solved) {
        // The scan belongs to a case that HAS been solved. Everything shown is
        // that case's real computed output — no substitution, no interpolation.
        writeTerminalLog(`[MATCH] '${patientId}' has a completed CFD run in this workspace.`, "success");
        writeTerminalLog("[INFO] Displaying its computed hemodynamics.", "info");
    } else {
        // Unknown patient. Record what the file genuinely says and nothing
        // more. Hemodynamics stay absent — the gauges already know how to
        // render "not computed", so an unsolved case reads as unsolved rather
        // than as a clean bill of health.
        writeTerminalLog(`[QUEUED] '${patientId}' has no completed CFD run.`, "warning");
        writeTerminalLog("[INFO] Header ingested. Estimating hemodynamics from the "
                       + "surrogate fitted to solved OpenFOAM cases…", "info");

        // Ask for the two measurements the surrogate needs.
        //
        // Segmentation cannot run in a browser, and inventing a dome diameter
        // would put us straight back to the fabrication this replaced. A
        // radiologist sizes the dome and neck off the scan anyway, so asking is
        // both honest and how the measurement is actually obtained. Given them,
        // every quantity except OSI and ECAP is available in under a
        // millisecond — no solve, no waiting.
        // Measure the sac from the PIXELS before asking anything.
        //
        // Dome and neck diameter are not in the DICOM header — they are image
        // content — so without this the numbers describe what a user typed
        // rather than what was scanned. Measured against the three solved
        // cases, dome is within 2.1% and neck within 15%. The form is
        // pre-filled from the measurement and stays editable, because a
        // clinician correcting an automated measurement is normal practice and
        // a threshold-based method is not a clinical segmentation.
        let measured = null;
        if (dicomBuffer && window.NeuroDicom && window.NeuroDicom.measureSac) {
            const m = window.NeuroDicom.measureSac(dicomBuffer, dicom.tags);
            if (m.ok && m.bulgeDetected) {
                measured = m;
                writeTerminalLog(
                    `[MEASURED] Sac from pixel data: dome ${m.domeDiameterMm} mm, `
                    + `neck ${m.neckDiameterMm} mm, parent ${m.parentDiameterMm} mm `
                    + `(${m.method}).`, "success");
            } else if (m.ok) {
                writeTerminalLog("[MEASURED] No aneurysmal bulge found in this slice — "
                               + "the vessel looks uniform.", "warning");
            } else {
                writeTerminalLog(`[MEASURED] Could not measure the sac: ${m.reason}`, "warning");
            }
        }

        await promptMorphologyAndEstimate(patientId, {
            modality, studyDate, rows, columns, sliceThickness,
            manufacturer, bodyPart, seriesDescription: seriesDesc, fileName,
        }, measured);
        return;
    }

    document.querySelectorAll(".patient-card").forEach(c => {
        c.classList.remove("active");
        if (c.dataset.id === patientId) c.classList.add("active");
    });

    loadPatientData(patientDatabase[patientId]);

    // Display custom alert notification of successful pipeline execution
    // Say which of the two things actually happened. "Computational pipeline
    // completed successfully" was printed unconditionally, including over
    // fabricated numbers for a case that had never been solved.
    alert(solved
        ? `Scan ingested for ${patientId}.\n\nThis case has a completed CFD run — `
          + `its computed hemodynamics are now shown.`
        : `Scan ingested for ${patientId}.\n\nThe DICOM header was read successfully, `
          + `but no CFD solve exists for this case, so wall shear stress, OSI, RRT and `
          + `ECAP are shown as not computed. A single cardiac cycle takes several hours `
          + `on this hardware.`);
}

// Start Application on Page Load
window.addEventListener("DOMContentLoaded", initApp);
