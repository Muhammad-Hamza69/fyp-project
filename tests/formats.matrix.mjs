/**
 * Every supported upload format, every displayed parameter, from the file.
 *
 * WHY THIS FILE EXISTS
 * The question it answers is the one that keeps coming back: if I upload a
 * different file, does the dashboard actually recompute, or is it showing me a
 * constant? Answering it by reading the code has failed twice. The OSI risk
 * band was calibrated on authored values, so a third of the risk index was
 * pinned to zero for every real case; and the sac's anatomical position fell
 * back to a hardcoded default while the panel described it as "recorded".
 * Both were invisible from the source and obvious the moment two different
 * files were pushed through and their outputs compared.
 *
 * So this pushes real files of every accepted type through the real readers and
 * the real surrogate, and asserts two things per parameter:
 *
 *   RESPONSIVE   different geometry gives a different value
 *   AGREEING     the same geometry gives the same value in every format
 *
 * The second matters as much as the first. The five fixtures encode the SAME
 * aneurysm in different containers, so a reader that disagrees with the others
 * on identical input is wrong, and nothing else would notice.
 *
 * Anything that CANNOT come from the file is asserted to say so rather than to
 * quietly produce a default — that is the failure mode this file exists for.
 *
 *     node tests/formats.matrix.mjs
 */

import { readFileSync, readdirSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const UPLOADS = resolve(ROOT, "test-uploads");

let failures = 0;
const check = (name, ok, detail = "") => {
    if (!ok) failures += 1;
    console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${ok || !detail ? "" : `\n          ${detail}`}`);
};

// --- a browser-shaped environment ------------------------------------------
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    runScripts: "outside-only",
});
const window = dom.window;
// The .gz readers need these. Node has them globally; jsdom's window does not,
// and without them the gzip formats fail for a reason that has nothing to do
// with the code under test.
window.DecompressionStream = globalThis.DecompressionStream;
window.Blob = globalThis.Blob;
window.Response = globalThis.Response;

const surrogateModel = JSON.parse(
    readFileSync(resolve(ROOT, "models/surrogate.json"), "utf8"));
window.fetch = async () => ({
    ok: true, status: 200, json: async () => surrogateModel, text: async () => "",
});

for (const f of ["thresholds.js", "dicom.js", "readers.js", "surrogate.js"]) {
    window.eval(readFileSync(resolve(ROOT, f), "utf8"));
}
await window.NeuroSurrogate.load();

const T = window.NeuroThresholds;
const brainMeta = JSON.parse(readFileSync(resolve(ROOT, "models/brain.json"), "utf8"));

/** The Composite Risk Index, with the same weights and bands app.js uses. */
function composite(dome, ar, tawss, osi) {
    const tawssScore = (1 - T.band(tawss, T.TAWSS_RISK_LOW_PA, T.TAWSS_RISK_HIGH_PA)) * 100;
    const osiScore = T.band(osi, T.OSI_RISK_LOW, T.OSI_RISK_HIGH) * 100;
    const diameterScore = Math.max(0, Math.min(1, (dome - 2.0) / 8.0)) * 100;
    const aspectScore = Math.max(0, Math.min(1, (ar - 0.7) / 1.8)) * 100;
    return {
        composite: Math.round(tawssScore * 0.35 + osiScore * 0.30
                            + diameterScore * 0.20 + aspectScore * 0.15),
        osiScore,
    };
}

/** Where the 3D sac gets drawn, and whether the file said so. */
function siteFor(clinical) {
    const raw = String((clinical || {}).site || "").trim().toUpperCase();
    const key = brainMeta.site_aliases?.[raw] || raw;
    return brainMeta.sites[key]
        ? { key, fromFile: true }
        : { key: brainMeta.default_site, fromFile: false };
}

async function analyse(name, bytes) {
    const ab = new ArrayBuffer(bytes.length);
    new Uint8Array(ab).set(bytes);
    const r = await window.NeuroReaders.read(name, ab);
    if (!r.ok) return { ok: false, reason: r.reason };

    const m = r.measurement;
    const h = window.NeuroSurrogate.predict({
        maxDiameterMm: m.domeDiameterMm,
        neckDiameterMm: m.neckDiameterMm,
        aspectRatio: m.aspectRatio,
    });
    const c = composite(m.domeDiameterMm, m.aspectRatio || 1, h.sacTawss, h.osi);
    return {
        ok: true, measurement: m, clinical: r.clinical || {}, hemo: h,
        cri: c.composite, osiScore: c.osiScore, site: siteFor(r.clinical),
    };
}

// --- 1. every format reads -------------------------------------------------
console.log("\nformats");
const files = readdirSync(UPLOADS).sort();
const results = new Map();
for (const f of files) {
    const r = await analyse(f, readFileSync(resolve(UPLOADS, f)));
    results.set(f, r);
    check(`${f} parses and measures`, r.ok, r.reason);
}

const EXTS = [".dcm", ".nii", ".nii.gz", ".stl", ".cas", ".cas.gz"];
for (const ext of EXTS) {
    const got = files.filter((f) => f.toLowerCase().endsWith(ext));
    check(`${ext} is covered by a fixture`, got.length > 0);
}

// --- 2. every parameter responds to the geometry ---------------------------
//
// Distinct GEOMETRIES, not distinct files: two fixtures encoding the same
// aneurysm in different containers are supposed to agree.
console.log("\nresponds to the file");
const byGeometry = new Map();
for (const [f, r] of results) {
    if (!r.ok) continue;
    byGeometry.set(r.measurement.domeDiameterMm.toFixed(2), { f, r });
}
const distinct = [...byGeometry.values()];
check("fixtures cover more than one geometry", distinct.length >= 3,
      `${distinct.length} distinct dome diameter(s)`);

for (const [label, get, dp] of [
    ["max diameter", (r) => r.measurement.domeDiameterMm, 2],
    ["aspect ratio", (r) => r.measurement.aspectRatio, 2],
    ["TAWSS", (r) => r.hemo.sacTawss, 3],
    ["OSI", (r) => r.hemo.osi, 3],
    ["ECAP", (r) => r.hemo.ecap, 3],
    ["RRT", (r) => r.hemo.rrt, 2],
    ["Composite Risk Index", (r) => r.cri, 0],
]) {
    const vals = distinct.map(({ r }) => Number(get(r)).toFixed(dp));
    check(`${label} differs between geometries at displayed precision`,
          new Set(vals).size > 1, `${vals.join(", ")}`);
}

// The OSI risk term specifically: it was clamped to 0 for every real case for
// months, so "it varies" is not enough — it has to be strictly inside the band.
for (const { f, r } of distinct) {
    check(`OSI risk term is live for ${f}`,
          r.osiScore > 0 && r.osiScore < 100,
          `scored ${r.osiScore.toFixed(1)}%`);
}

// --- 3. the same aneurysm reads the same in every container ----------------
//
// PT-2026-0401_SURFACE.stl and PT-2026-0404/0405 encode identical vertices, and
// PT-2026-0402/0403 are the same volume as PT-2026-0302. A reader that
// disagrees on identical input is wrong.
console.log("\ncross-format agreement");
for (const group of [
    ["PT-2026-0401_SURFACE.stl", "PT-2026-0404_MESH.cas", "PT-2026-0405_MESH.cas.gz"],
    ["PT-2026-0402_VOLUME.nii", "PT-2026-0403_VOLUME.nii.gz"],
]) {
    const present = group.filter((f) => results.get(f)?.ok);
    if (present.length < 2) {
        check(`group ${group[0]} has at least two readable members`, false,
              `only ${present.length} readable`);
        continue;
    }
    const domes = present.map((f) => results.get(f).measurement.domeDiameterMm);
    const spread = Math.max(...domes) - Math.min(...domes);
    check(`${present.join(" = ")} measure the same dome`,
          spread < 0.01, `domes ${domes.join(", ")}`);

    const cris = present.map((f) => results.get(f).cri);
    check(`  ...and score the same risk index`, new Set(cris).size === 1,
          `CRI ${cris.join(", ")}`);
}

// --- 4. what the file cannot supply must SAY SO ----------------------------
//
// The two things a bare geometry file genuinely cannot carry are clinical
// history and anatomical position. Both previously produced a plausible default
// instead: PHASES was scored from five invented inputs, and the sac was drawn
// at MCA under a caption reading "positioned at its recorded site".
console.log("\nabsent data is reported absent, not defaulted");
{
    // Blank the 80-byte STL header — a plain surface from any other tool.
    const annotated = readFileSync(resolve(UPLOADS, "PT-2026-0401_SURFACE.stl"));
    const plain = Buffer.from(annotated);
    plain.fill(0, 0, 80);

    const r = await analyse("anonymous_tree.stl", plain);
    check("an unannotated STL still measures", r.ok, r.reason);
    if (r.ok) {
        // Geometry is still real — stripping the annotation must not change it.
        const ref = results.get("PT-2026-0401_SURFACE.stl");
        check("stripping the annotation does not change the measurement",
              r.measurement.domeDiameterMm === ref.measurement.domeDiameterMm,
              `${r.measurement.domeDiameterMm} vs ${ref.measurement.domeDiameterMm}`);

        // Clinical history genuinely is not in the file.
        for (const k of ["age", "hypertension", "earlierSAH", "population", "site"]) {
            check(`  ${k} is absent, not defaulted`,
                  r.clinical[k] === undefined || r.clinical[k] === null,
                  `got ${JSON.stringify(r.clinical[k])}`);
        }

        // And the position is flagged as a placeholder rather than "recorded".
        check("sac position is marked as NOT from the file",
              r.site.fromFile === false,
              `resolved ${r.site.key}, fromFile=${r.site.fromFile}`);
    }

    // Where the file DOES record a site, it must be used, not the default.
    const withSite = results.get("PT-2026-0303_MRA.dcm");
    if (withSite?.ok) {
        check("a recorded site is read from the file and is not the default",
              withSite.site.fromFile === true
              && withSite.site.key !== brainMeta.default_site,
              `resolved ${withSite.site.key}, default ${brainMeta.default_site}`);
    }
}

// --- 5. neuro3d.js must actually publish that distinction ------------------
{
    const js = readFileSync(resolve(ROOT, "neuro3d.js"), "utf8");
    check("neuro3d.js reports whether the site came from the file",
          js.includes("fromFile") && js.includes("neuro-3d-sac-site"),
          "the placeholder position would be captioned as recorded");
    const html = readFileSync(resolve(ROOT, "index.html"), "utf8");
    check("the 3D panel has an element to say it in",
          html.includes('id="neuro-3d-sac-site"'));
}

console.log(failures ? `\n${failures} failure(s)` : "\nall checks passed");
process.exit(failures ? 1 : 0);
