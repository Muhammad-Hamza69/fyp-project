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

// --- 2b. every risk tier must be reachable ---------------------------------
//
// This is the check that would have caught both band bugs, and it is the one
// that was missing each time.
//
// A normalisation band whose range does not overlap the data that flows through
// it turns a weighted term into a constant, silently and permanently. It has
// happened twice here in opposite directions: the OSI band's FLOOR sat above
// every solved value so the term pinned at 0, and the TAWSS band's CEILING sat
// above every solved value so the term pinned near 100. The visible symptom of
// the second was that all six cases on the site read "Moderate" no matter what
// their geometry was — the index could only reach 42.5 to 75.8 across the
// ENTIRE geometry space, and 45 to 75 was the Moderate band.
//
// So: sweep the whole geometry space, and assert the index actually produces
// all three tiers. Not that the boundaries are particular numbers — that every
// verdict the interface can display is one some input can cause.
console.log("\nevery risk tier is reachable");
{
    const S = window.NeuroSurrogate;
    let lo = Infinity, hi = -Infinity;
    const tiers = new Set();

    for (let dome = 2; dome <= 30; dome += 0.5) {
        for (let ar = 0.5; ar <= 3.5; ar += 0.1) {
            const h = S.predict({ maxDiameterMm: dome, neckDiameterMm: dome / ar,
                                  aspectRatio: ar });
            const c = composite(dome, ar, h.sacTawss, h.osi).composite;
            lo = Math.min(lo, c); hi = Math.max(hi, c);
            tiers.add(c >= T.CRI_HIGH ? "High" : c >= T.CRI_MODERATE ? "Moderate" : "Low");
        }
    }

    check("the index reaches Low somewhere", tiers.has("Low"),
          `reachable range ${lo}-${hi}, boundary ${T.CRI_MODERATE}`);
    check("the index reaches Moderate somewhere", tiers.has("Moderate"),
          `reachable range ${lo}-${hi}`);
    check("the index reaches High somewhere", tiers.has("High"),
          `reachable range ${lo}-${hi}, boundary ${T.CRI_HIGH} — `
          + `no geometry can produce a High verdict`);

    // And the boundaries must sit INSIDE the reachable range, not merely be
    // crossable at one extreme corner of it.
    check("the Moderate boundary is inside the reachable range",
          T.CRI_MODERATE > lo && T.CRI_MODERATE < hi, `${lo} < ${T.CRI_MODERATE} < ${hi}`);
    check("the High boundary is inside the reachable range",
          T.CRI_HIGH > lo && T.CRI_HIGH < hi, `${lo} < ${T.CRI_HIGH} < ${hi}`);

    // Each individual term must vary too. A term pinned at either end is a
    // constant wearing a weight, which is exactly how both bugs presented.
    for (const [label, lo_, hi_, get] of [
        ["TAWSS", T.TAWSS_RISK_LOW_PA, T.TAWSS_RISK_HIGH_PA, (h) => h.sacTawss],
        ["OSI", T.OSI_RISK_LOW, T.OSI_RISK_HIGH, (h) => h.osi],
    ]) {
        const scores = [];
        for (let dome = 3; dome <= 12; dome += 0.5) {
            const h = S.predict({ maxDiameterMm: dome, neckDiameterMm: dome * 0.8 });
            scores.push(T.band(get(h), lo_, hi_));
        }
        const spread = Math.max(...scores) - Math.min(...scores);
        check(`the ${label} term is not pinned to an endpoint`, spread > 0.15,
              `spans only ${(spread * 100).toFixed(1)}% of its band across `
              + `3-12 mm domes — it is a constant, not a variable`);
    }
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

// --- 5. the distinction must survive the panel that used to show it --------
//
// The 3D provenance caption was removed from the interface. The FACT it carried
// did not go with it: the sac's size and shape are measured from the file, but
// its position is only from the file when the file records an aneurysm site.
// resolveSite still resolves that, the viewer still exposes it, and the case
// report no longer prints a named artery when nothing named one.
{
    const js = readFileSync(resolve(ROOT, "neuro3d.js"), "utf8");
    check("neuro3d.js still resolves whether the site came from the file",
          js.includes("siteFromFile") && js.includes("fromFile:"),
          "the placeholder position would be indistinguishable from a recorded one");
    check("the viewer exposes what the sac was built from",
          js.includes("sacInfo"), "nothing downstream could ask");

    const app = readFileSync(resolve(ROOT, "app.js"), "utf8");
    check("the report does not name an artery the file never recorded",
          app.includes("Not recorded in the supplied file"),
          "reportAnatomicalTargetEl would print a default site as a finding");
}

// --- 6. the reference page ------------------------------------------------
//
// A static page has ways of being broken that a browser never reports: anchors
// pointing at nothing, a formula that swallowed itself because its MathML was
// malformed, a stylesheet that was never linked. None of them throw, and the
// page looks fine at a glance in every case.
console.log("\nhemodynamic reference page");
{
    const html = readFileSync(resolve(ROOT, "hemodynamics.html"), "utf8");
    const page = new JSDOM(html).window.document;

    for (const id of ["physics", "wss", "derived", "tawss", "osi", "rrt",
                      "ecap", "summary", "inpractice"]) {
        check(`section #${id} exists`, page.getElementById(id) !== null);
    }

    const broken = [...page.querySelectorAll(".reference-toc a")]
        .map((a) => a.getAttribute("href").slice(1))
        .filter((id) => !page.getElementById(id));
    check("every contents link resolves", broken.length === 0, broken.join(", "));

    // The formulas are the point of the page. MathML renders natively so there
    // is no library to fail — but a malformed expression drops its own tail
    // silently, and the surrounding prose still reads correctly.
    const maths = [...page.querySelectorAll("math")];
    check("formulas are present as MathML", maths.length >= 12,
          `${maths.length} <math> elements`);
    check("no formula is empty",
          maths.every((m) => (m.textContent || "").trim().length > 0));

    check("the page links back to the dashboard", html.includes('href="index.html"'));
    check("the page loads the shared design tokens",
          html.includes('href="style.css"') && html.includes('href="reference.css"'));

    const dash = readFileSync(resolve(ROOT, "index.html"), "utf8");
    check("the dashboard links to the reference page",
          dash.includes('href="hemodynamics.html"'),
          "the page would be unreachable from the app");
}

console.log(failures ? `\n${failures} failure(s)` : "\nall checks passed");
process.exit(failures ? 1 : 0);
