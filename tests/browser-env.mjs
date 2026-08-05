/**
 * The browser globals jsdom does not reliably provide.
 *
 * WHY THIS FILE EXISTS
 * CI went red for five consecutive commits with
 *
 *     ReferenceError: TextDecoder is not defined
 *         at readFluentNodes (readers.js)
 *
 * while the same test passed locally every time. The code was fine. The two
 * environments were running DIFFERENT LIBRARIES: package.json asks for
 * `jsdom ^25.0.0`, the lockfile pins 25.0.1, and CI installs that with
 * `--frozen-lockfile` — but the local node_modules had drifted to 29.1.1.
 * jsdom 29 exposes TextDecoder on the window; jsdom 25 does not.
 *
 * So a green local run was not evidence of anything, and the real lesson is not
 * "add TextDecoder": it is that the harness must not depend on which globals a
 * particular jsdom release happens to ship. Node has all of these natively.
 * Install them explicitly, and fail loudly and early if one is missing rather
 * than several seconds later inside a file reader, where the stack points at
 * production code that is not at fault.
 *
 * Anything the code under test needs from `window` belongs in REQUIRED below.
 */

/**
 * Browser globals the readers, parsers and surrogate genuinely use.
 * Each entry names the consumer, so an unexplained addition is visible.
 */
const REQUIRED = [
    ["TextDecoder", "readers.js — decoding Fluent .cas text and STL headers"],
    ["TextEncoder", "readers.js — annotation round-trips"],
    ["atob", "dicom.js — base64 in a few tag paths"],
    ["btoa", "dicom.js"],
];

/**
 * Globals that must all come from the SAME implementation, not merely exist.
 *
 * The gunzip path in readers.js is `new Response(blob.stream()).body
 * .pipeThrough(new DecompressionStream("gzip"))`. jsdom 25 ships its own Blob,
 * which satisfies a `typeof` check but has no `.stream()`, and even where it
 * does, a jsdom Blob and a Node Response are different realms and refuse to
 * interoperate:
 *
 *     could not decompress: (intermediate value).stream is not a function
 *
 * Filling only what is MISSING is therefore wrong here — it leaves jsdom's Blob
 * in place beside Node's Response and produces a failure that reads like a
 * broken reader. These four are overwritten as a set so they match each other.
 */
const COHERENT_SET = [
    ["Blob", "readers.js — gunzip via Response(Blob.stream())"],
    ["Response", "readers.js — gunzip"],
    ["ReadableStream", "readers.js — gunzip"],
    ["DecompressionStream", "readers.js — .nii.gz and .cas.gz"],
];

/**
 * Give a jsdom window the globals a real browser has.
 *
 * @param {object} window  a jsdom window
 * @returns {string[]}     the names that were installed, for reporting
 */
export function installBrowserGlobals(window) {
    const filled = [];

    // Fill only what is absent: a jsdom that provides one keeps its own, since
    // replacing it wholesale can break the instanceof checks jsdom does
    // internally.
    for (const [name] of REQUIRED) {
        if (typeof window[name] === "undefined" && typeof globalThis[name] !== "undefined") {
            window[name] = globalThis[name];
            filled.push(name);
        }
    }

    // Replace as a set, present or not, so they belong to one realm.
    for (const [name] of COHERENT_SET) {
        if (typeof globalThis[name] !== "undefined") {
            window[name] = globalThis[name];
            filled.push(name);
        }
    }

    const missing = [...REQUIRED, ...COHERENT_SET]
        .filter(([name]) => typeof window[name] === "undefined")
        .map(([name, why]) => `${name} (${why})`);
    if (missing.length) {
        throw new Error(
            "The test environment cannot provide these browser globals, so the "
            + "code under test cannot run:\n  - " + missing.join("\n  - ")
            + `\n\nNode ${process.version} does not expose them either. This is an `
            + "environment problem, not a product bug."
        );
    }
    return filled;
}

/**
 * Report the versions the run actually used.
 *
 * Printed by both suites because "it passed on my machine" was, on this
 * project, literally true and completely uninformative. Seeing jsdom 25.0.1 in
 * the CI log next to 29.1.1 locally is what identified the problem.
 */
/**
 * Fail if the installed jsdom is not the one the lockfile pins.
 *
 * This is the check that would have saved five red CI runs. The suites passed
 * locally against jsdom 29.1.1 while CI installed 25.0.1 from the frozen
 * lockfile, so a green local run carried no information about CI at all — and
 * the divergence was invisible because nothing ever printed either version.
 *
 * A warning rather than a hard failure: someone deliberately testing an upgrade
 * should not be blocked, but they should not be able to miss it either.
 */
export async function warnOnDependencyDrift() {
    const { createRequire } = await import("node:module");
    const require = createRequire(import.meta.url);
    let installed, declared;
    try {
        installed = require("jsdom/package.json").version;
        declared = require("../package.json").devDependencies.jsdom;
    } catch { return null; }

    // `^25.0.0` -> major 25
    const wantMajor = declared.replace(/[^0-9.]/g, "").split(".")[0];
    const gotMajor = installed.split(".")[0];
    if (wantMajor !== gotMajor) {
        console.warn(
            `
  !! jsdom ${installed} is installed but package.json asks for `
            + `${declared}.
     CI installs the lockfile version, so a green run here `
            + `predicts nothing.
     Run: pnpm install --frozen-lockfile
`
        );
        return { installed, declared };
    }
    return null;
}

export async function environmentBanner() {
    let jsdomVersion = "unknown";
    try {
        const { createRequire } = await import("node:module");
        jsdomVersion = createRequire(import.meta.url)("jsdom/package.json").version;
    } catch { /* not fatal — the banner is diagnostic, not a gate */ }
    return `node ${process.version}, jsdom ${jsdomVersion}`;
}
