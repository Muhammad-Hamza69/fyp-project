/**
 * Manual end-to-end upload probe.
 *
 * Drives a real DICOM file through the whole upload flow in jsdom: file
 * change event -> scripted animation -> pixel measurement -> measurement form
 * -> surrogate -> patient added to the list.
 *
 * NOT part of CI: the scripted animation sleeps for ~40 s, which is too slow
 * to run on every push. The regressions it found are pinned structurally in
 * dashboard.smoke.mjs instead. Run this by hand after touching the upload
 * path:
 *
 *   node tests/upload.probe.mjs
 */
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dom = new JSDOM(readFileSync(`${ROOT}/index.html`, "utf8"), {
    runScripts: "outside-only", pretendToBeVisual: true,
    url: "https://neuroflow-cfd.vercel.app/",
});
const { window } = dom;

const ctx = new Proxy({}, { get: (t, p) => {
    if (p === "canvas") return { width: 800, height: 600 };
    if (p === "createLinearGradient" || p === "createRadialGradient") return () => ({ addColorStop() {} });
    if (p === "measureText") return () => ({ width: 10 });
    if (p === "getImageData") return () => ({ data: new Uint8ClampedArray(4) });
    return () => {};
}, set: () => true });
window.HTMLCanvasElement.prototype.getContext = () => ctx;

const pats = JSON.parse(readFileSync(`${ROOT}/real-cfd-patients.json`, "utf8"));
const surro = JSON.parse(readFileSync(`${ROOT}/models/surrogate.json`, "utf8"));
window.fetch = async (u) => ({
    ok: true, status: 200,
    json: async () => (String(u).includes("surrogate") ? surro : pats),
    text: async () => "",
});

const errors = [];
window.addEventListener("error", (e) => errors.push(String(e.error || e.message)));
window.console.error = (...a) => {
    const s = a.map(String).join(" ");
    if (!/NeuroViewer undefined/.test(s)) errors.push(s);
};
window.alert = (m) => console.log("  ALERT:", String(m).split("\n")[0]);

window.eval(readFileSync(`${ROOT}/dicom.js`, "utf8"));
window.eval(readFileSync(`${ROOT}/surrogate.js`, "utf8"));
window.eval(readFileSync(`${ROOT}/app.js`, "utf8"));
window.document.dispatchEvent(new window.Event("DOMContentLoaded"));
await new Promise((r) => setTimeout(r, 300));

const $ = (id) => window.document.getElementById(id);
const t0 = Date.now();
const before = window.document.querySelectorAll(".patient-card").length;
console.log(`  patient cards before upload: ${before}`);

// A File whose arrayBuffer() returns the real DICOM bytes.
const bytes = readFileSync(`${ROOT}/test-uploads/PT-2026-0303_MRA.dcm`);
const ab = new window.ArrayBuffer(bytes.length);
new window.Uint8Array(ab).set(bytes);
const file = { name: "PT-2026-0303_MRA.dcm", size: bytes.length,
               arrayBuffer: async () => ab };

const uploader = $("file-uploader");
console.log(`  #file-uploader present: ${!!uploader}`);

Object.defineProperty(uploader, "files", { value: [file], configurable: true });
uploader.dispatchEvent(new window.Event("change"));

// The flow has scripted sleeps; give it time, and report progress.
let shown = false;
for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 500));
    const c = $("morphology-prompt");
    if (c && !c.classList.contains("hidden")) { shown = true; break; }
}
const term = $("terminal-log-output");
if (term) {
    const lines = term.textContent.split(String.fromCharCode(10)).map(s => s.trim()).filter(Boolean);
    console.log(`  terminal lines: ${lines.length}`);
    console.log("  last 6:"); lines.slice(-6).forEach(l => console.log("    " + l));
}
const modalOpen = $("simulation-modal") && !$("simulation-modal").classList.contains("hidden");
console.log(`  modal open when form shown: ${modalOpen}   <- must be true or the form is invisible`);

const card = $("morphology-prompt");
console.log(`  time to form: ${((Date.now()-t0)/1000).toFixed(1)} s`);
console.log(`  morphology prompt visible: ${card && !card.classList.contains("hidden")}`);
console.log(`  dome prefilled: ${$("morph-dome") ? $("morph-dome").value : "n/a"}`);
console.log(`  neck prefilled: ${$("morph-neck") ? $("morph-neck").value : "n/a"}`);

if (card && !card.classList.contains("hidden")) {
    $("morph-compute").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 600));
}

const after = window.document.querySelectorAll(".patient-card").length;
const ids = [...window.document.querySelectorAll(".patient-card")].map((c) => c.dataset.id);
console.log(`  patient cards after: ${after}`);
console.log(`  contains PT-2026-0303: ${ids.includes("PT-2026-0303")}`);
if (errors.length) console.log("  ERRORS:\n    " + errors.join("\n    "));
