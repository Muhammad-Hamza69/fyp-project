/**
 * Dashboard smoke test — loads index.html + app.js in a real DOM and drives it.
 *
 * WHY THIS EXISTS
 * A one-word typo (`patient` where the function's scope only had
 * `activePatient`) threw a ReferenceError partway through updateRadialGauges().
 * Everything downstream died with it: the OSI, RRT and ECAP gauges, then
 * renderPhasesScore() and renderMlPrediction() further along the call chain,
 * and finally the click handlers bound after the first render — so Upload
 * DICOM, 3D Nerve Model, OSI Instability and Expand Case Review all stopped
 * responding.
 *
 * The suite had 116 tests at the time and not one of them noticed, because
 * every single one was Python or a string grep over the source. Nothing ever
 * EXECUTED the page. Gauges that never ran kept their previous markup, so the
 * failure rendered as a confident "0.00" rather than an error — the worst
 * possible presentation, and invisible to any test that does not run the code.
 *
 *   node tests/dashboard.smoke.mjs
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

let failures = 0;
const check = (name, cond, detail = "") => {
    if (cond) {
        console.log(`  PASS  ${name}`);
    } else {
        failures++;
        console.log(`  FAIL  ${name}${detail ? `\n          ${detail}` : ""}`);
    }
};

const html = readFileSync(resolve(ROOT, "index.html"), "utf8");
const appJs = readFileSync(resolve(ROOT, "app.js"), "utf8");
const patients = JSON.parse(readFileSync(resolve(ROOT, "real-cfd-patients.json"), "utf8"));

const dom = new JSDOM(html, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    url: "https://neuroflow-cfd.vercel.app/",
});
const { window } = dom;

// Canvas is not implemented in jsdom; the heatmap only needs a 2D context that
// accepts calls. Stubbing it keeps the test focused on logic rather than pixels.
const ctxStub = new Proxy({}, {
    get: (_t, prop) => {
        if (prop === "canvas") return { width: 800, height: 600 };
        if (prop === "createLinearGradient" || prop === "createRadialGradient") {
            return () => ({ addColorStop() {} });
        }
        if (prop === "measureText") return () => ({ width: 10 });
        if (prop === "getImageData") return () => ({ data: new Uint8ClampedArray(4) });
        return () => {};
    },
    set: () => true,
});
window.HTMLCanvasElement.prototype.getContext = () => ctxStub;

// Serve the computed cases the way the page fetches them.
window.fetch = async (url) => ({
    ok: true,
    status: 200,
    json: async () => (String(url).includes("real-cfd-patients") ? patients : {}),
    text: async () => "",
});

// Record anything thrown asynchronously or logged as an error — a listener that
// throws on click fails silently otherwise.
// Exactly one message is tolerated, and only this one. neuro3d.js is an ES
// module that imports three.js; jsdom runs scripts "outside-only" and has no
// WebGL, so the viewer genuinely cannot load here. The page detects that and
// logs this diagnostic instead of crashing — which is the behaviour under test,
// not a fault. Everything else counts as a failure, because a blanket filter
// would have hidden the very ReferenceError this file exists to catch.
const EXPECTED = [/window\.NeuroViewer undefined: neuro3d\.js did not execute/];
const errors = [];
const record = (msg) => {
    const s = String(msg);
    if (!EXPECTED.some((re) => re.test(s))) errors.push(s);
};
window.addEventListener("error", (e) => record(e.error || e.message));
window.console.error = (...a) => record(a.map(String).join(" "));

// --- execute the page -------------------------------------------------------
try {
    window.eval(appJs);
} catch (err) {
    check("app.js evaluates without throwing", false, String(err && err.stack || err));
    console.log(`\n${failures} failure(s)`);
    process.exit(1);
}
check("app.js evaluates without throwing", true);

// Initialisation runs here. A throw inside it (the original bug) would
// otherwise propagate out and kill the process with a raw stack trace before
// any result was printed — a correct non-zero exit, but an unreadable one.
try {
    window.document.dispatchEvent(new window.Event("DOMContentLoaded"));
    check("page initialises without throwing", true);
} catch (err) {
    check("page initialises without throwing", false, String(err && err.stack || err));
    console.log(`\n${failures} failure(s) — initialisation threw, so nothing below ran`);
    process.exit(1);
}
await new Promise((r) => setTimeout(r, 250));   // let the fetch settle

const $ = (id) => window.document.getElementById(id);
const txt = (id) => ($(id) ? ($(id).textContent || "").trim() : null);

// --- the gauges that died ---------------------------------------------------
//
// Each of these sits AFTER the throw site, so each was left holding stale
// markup. Asserting they are non-empty is what catches a repeat.
for (const [id, label] of [
    ["composite-risk-score", "Composite Risk Index"],
    ["tawss-gauge-val", "TAWSS"],
    ["osi-gauge-val", "OSI"],
    ["rrt-gauge-val", "RRT"],
    ["ecap-gauge-val", "ECAP"],
    ["phases-total-points", "PHASES points"],
]) {
    const v = txt(id);
    check(`${label} renders`, v !== null && v !== "" && v !== "-",
          `#${id} = ${JSON.stringify(v)}`);
}

// RRT is computed from TAWSS and OSI and is defined on a steady solve, so
// unlike OSI/ECAP it must be a real number for every case. It read 0.00 while
// the bug was live, which is why it is checked explicitly.
const rrt = parseFloat(txt("rrt-gauge-val"));
check("RRT is a real value, not 0.00", Number.isFinite(rrt) && rrt > 0,
      `rrt-gauge-val = ${txt("rrt-gauge-val")}`);

// PHASES is rendered by a function called after the throw site.
const phasesRows = $("phases-breakdown");
check("PHASES breakdown is populated",
      !!phasesRows && phasesRows.children.length > 0,
      `#phases-breakdown children = ${phasesRows ? phasesRows.children.length : "n/a"}`);

// --- the AI card ------------------------------------------------------------
const mlCard = $("ml-card");
const activeIsComputed = (txt("composite-risk-score") || "") !== "";
if (mlCard && !mlCard.classList.contains("hidden")) {
    check("AI probability renders", (txt("ml-probability") || "—") !== "—",
          `ml-probability = ${txt("ml-probability")}`);
    check("AI validity caveat is present",
          ($("ml-validity")?.textContent || "").toLowerCase().includes("synthetic"));
    check("AI SHAP bars render", ($("ml-shap")?.children.length || 0) > 0);
} else {
    check("AI card hidden only when the case has no prediction", activeIsComputed === false,
          "ml-card is hidden on a case that should have a prediction");
}

// --- the buttons that stopped responding ------------------------------------
//
// Listeners are attached during initialisation. When the first render threw,
// binding never completed and every one of these went dead. Clicking each and
// asserting no error is what would have caught the report.
const beforeClicks = errors.length;
for (const [id, label] of [
    ["view-3d-btn", "3D Nerve Model"],
    ["toggle-osi-btn", "OSI Instability"],
    ["toggle-tawss-btn", "TAWSS Distribution"],
    ["view-2d-btn", "2D Heatmap"],
    ["expand-case-btn", "Expand Case Review"],
    ["sidebar-upload-box", "Upload DICOM / MRA"],
]) {
    const el = $(id);
    if (!el) { check(`${label} exists`, false, `#${id} not found in index.html`); continue; }
    try {
        el.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
        check(`${label} click handled`, true);
    } catch (err) {
        check(`${label} click handled`, false, String(err));
    }
}
check("no errors raised while clicking", errors.length === beforeClicks,
      errors.slice(beforeClicks).join("\n          "));

// --- switching cases must not throw -----------------------------------------
const cards = window.document.querySelectorAll(".patient-card, [data-patient-id]");
if (cards.length) {
    const before = errors.length;
    for (const card of cards) {
        card.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
    }
    check("switching between every case raises no error",
          errors.length === before, errors.slice(before).join("\n          "));
    check("gauges still populated after switching",
          (txt("rrt-gauge-val") || "") !== "" && (txt("phases-total-points") || "") !== "");
}

// --- RRT / ECAP must match the solver, not be recomputed from means ---------
//
// Both are non-linear in TAWSS and OSI, so by Jensen's inequality evaluating
// them at the sac's MEAN shear is not the same as averaging them over the sac.
// The dashboard used to do the former while the PDF report and the methods
// document quoted the latter: 4.25 against 11.07, 7.10 against 21.35, 2.94
// against 7.17. The last of those sat below the 3.0 alert threshold, so a case
// whose true residence time should have flagged showed as normal.
{
    const pats = patients.patients || patients;
    const computed = pats.filter((p) => p.hemodynamics && p.hemodynamics.rrt > 0);
    check("cohort has solver RRT to compare against", computed.length > 0);

    for (const p of computed) {
        for (const card of window.document.querySelectorAll(".patient-card")) {
            if ((card.dataset.id || "") === p.id) {
                card.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
            }
        }
        const shown = parseFloat(txt("rrt-gauge-val"));
        check(`RRT matches the solver for ${p.id}`,
              Math.abs(shown - p.hemodynamics.rrt) < 0.02,
              `shown ${shown}, solver ${p.hemodynamics.rrt}`);

        if (p.hemodynamics.transient && p.hemodynamics.ecap > 0) {
            const e = parseFloat(txt("ecap-gauge-val"));
            check(`ECAP matches the solver for ${p.id}`,
                  Math.abs(e - p.hemodynamics.ecap) < 0.02,
                  `shown ${e}, solver ${p.hemodynamics.ecap}`);
        }
    }
}

// --- gauge notes sit under the card, not inside the ring --------------------
//
// `.radial-progress-text` is an absolutely-positioned overlay centred in an
// 80px doughnut. Appending the "steady solve — no cardiac cycle" caption there
// wrapped it into the middle of the ring, on top of the value it explains.
{
    const notes = window.document.querySelectorAll(".gauge-note");
    check("gauge notes render for uncomputed values", notes.length >= 0);
    let inRing = 0;
    for (const n of notes) {
        if (n.closest(".radial-progress-text") || n.closest(".radial-progress-container")) inRing++;
    }
    check("no gauge note is nested inside the progress ring", inRing === 0,
          `${inRing} note(s) inside .radial-progress-container`);
}

// --- the upload must not hide its own form ---------------------------------
//
// The sac-measurement form lives INSIDE the simulation modal. The modal used to
// be closed unconditionally at the end of the scripted animation, before the
// form was shown — so for a patient with no solved case the form had its
// `hidden` class removed on an element inside an already-hidden container.
// Invisible, unclickable, never confirmed, so the upload added nothing to the
// profile list and appeared to do nothing at all.
//
// Asserted structurally rather than by driving the whole upload: the animation
// runs for ~40 s of scripted sleeps, which does not belong in CI.
{
    const app = readFileSync(resolve(ROOT, "app.js"), "utf8");
    const body = app.split("async function runCfdSimulation")[1] || "";

    check("upload flow declares whether a measurement form is needed",
          /needsMeasurement/.test(body));

    // The close inside runCfdSimulation must be guarded.
    const closeIdx = body.indexOf('simulationModalEl.classList.add("hidden")');
    check("modal close in the upload flow is conditional", closeIdx > -1
          && /if \(!needsMeasurement\)\s*\{\s*$/m.test(body.slice(Math.max(0, closeIdx - 120), closeIdx)),
          "the modal is closed unconditionally — the measurement form would be invisible");

    // ...and the form's submit handler must close it, or the modal is stranded.
    const onGo = app.split("const onGo = ")[1] || "";
    check("submitting the measurement form closes the modal",
          /simulation-modal/.test(onGo.slice(0, 600)),
          "nothing closes the modal after the form is used");
}

// --- DICOM parsing --------------------------------------------------------
//
// The upload handler used to read files with readAsText() and regex out
// "metadata", which only worked on the ASCII stubs that used to ship as .dcm.
// These assertions pin that a real file is read from its bytes and a non-DICOM
// file is refused rather than silently replaced by hardcoded defaults.
{
    // Evaluated in the window, exactly as the <script> tag does in index.html.
    window.eval(readFileSync(resolve(ROOT, "dicom.js"), "utf8"));
    const D = window.NeuroDicom;
    check("dicom.js exposes a parser", !!(D && D.parse));

    if (D && D.parse) {
        // The buffer has to be allocated inside the jsdom realm: a Node
        // ArrayBuffer passed across realms is not recognised by that realm's
        // DataView, and the parser would fail for a reason that has nothing to
        // do with the file.
        const toBuf = (p) => {
            const b = readFileSync(resolve(ROOT, p));
            const ab = new window.ArrayBuffer(b.length);
            new window.Uint8Array(ab).set(b);
            return ab;
        };

        for (const [file, id] of [
            ["samples/PT-2026-0101_MRA_AXIAL.dcm", "PT-2026-0101"],
            ["samples/PT-2026-0103_MRA_AXIAL.dcm", "PT-2026-0103"],
        ]) {
            let r;
            try { r = D.parse(toBuf(file)); } catch (e) { r = { ok: false, reason: String(e) }; }
            check(`real DICOM parses: ${id}`, r.ok, r.reason || "");
            if (r.ok) {
                check(`  PatientID read from the header (${id})`, r.tags.patientID === id,
                      `got ${JSON.stringify(r.tags.patientID)}`);
                check(`  Modality read (${id})`, r.tags.modality === "MR");
                check(`  dimensions read (${id})`,
                      Number.isFinite(+r.tags.rows) && Number.isFinite(+r.tags.columns));
                check(`  slice thickness read (${id})`,
                      Number.isFinite(parseFloat(r.tags.sliceThickness)));
            }
        }

        // A plain-text file named .dcm — exactly what used to ship in this repo
        // and exactly what the old readAsText()+regex handler "parsed"
        // successfully. Synthesised here so the test carries no fixture.
        const stubText =
            "PATIENT_ID = PT-2025-0061\nMODALITY = MR\nROWS = 512\nCOLUMNS = 512\n"
            + "SLICE_THICKNESS = 0.5\nNUMBER_OF_SLICES = 142\n".repeat(6);
        const stubBuf = new window.ArrayBuffer(stubText.length);
        const bytes = new window.Uint8Array(stubBuf);
        for (let i = 0; i < stubText.length; i++) bytes[i] = stubText.charCodeAt(i);

        const stub = D.parse(stubBuf);
        check("ASCII stub is rejected, not parsed", stub.ok === false);
        check("  rejection explains why", /DICM/.test(stub.reason || ""),
              stub.reason || "");

        // Random bytes must not be mistaken for a scan.
        check("random bytes rejected", D.parse(new window.ArrayBuffer(4096)).ok === false);
        check("undersized file rejected", D.parse(new window.ArrayBuffer(8)).ok === false);
    }
}

check("page produced no uncaught errors overall", errors.length === 0,
      errors.join("\n          "));

console.log(failures ? `\n${failures} failure(s)` : "\nall checks passed");
process.exit(failures ? 1 : 0);
