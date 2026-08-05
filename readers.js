/**
 * Geometry readers — STL, NIfTI and Fluent case files, alongside DICOM.
 *
 * The upload path needs one thing from a file: the sac's dome and neck
 * diameter in millimetres. DICOM reaches that through pixel intensity
 * (dicom.js). These formats reach it differently, and the difference matters:
 *
 *   STL           an explicit triangulated SURFACE. The geometry is already
 *                 there, so no thresholding is involved and the measurement is
 *                 the most direct of the four.
 *   NIfTI         a labelled or intensity VOLUME, like DICOM. Thresholded the
 *                 same way, with the same caveats.
 *   Fluent .cas   a volume MESH. Only its node coordinates are read — enough
 *                 for geometry, and far short of parsing the format properly.
 *
 * WHAT NONE OF THEM CARRY
 * Clinical history. Age, hypertension, prior SAH, population and aneurysm site
 * exist in DICOM because DICOM is a clinical format with standard tags for
 * them. An STL is a bag of triangles; a Fluent case is a mesh. So PHASES stays
 * unscored for these, and says which inputs are missing — the same rule already
 * applied to a DICOM that omits them.
 *
 * Gzip is handled through DecompressionStream, which is native in current
 * browsers. Where it is unavailable the file is refused with that reason rather
 * than misparsed.
 */

(function (global) {
    "use strict";

    const FORMATS = {
        dicom: { ext: [".dcm", ".dicom"], label: "DICOM" },
        nifti: { ext: [".nii", ".nii.gz"], label: "NIfTI" },
        stl:   { ext: [".stl"], label: "STL surface" },
        fluent:{ ext: [".cas", ".cas.gz", ".msh"], label: "Fluent case" },
    };

    function detect(fileName, buffer) {
        const n = String(fileName || "").toLowerCase();
        for (const [key, f] of Object.entries(FORMATS)) {
            if (f.ext.some((e) => n.endsWith(e))) return key;
        }
        // Fall back to content sniffing when the extension is unhelpful.
        if (buffer && buffer.byteLength > 132) {
            const v = new DataView(buffer);
            let dicm = "";
            for (let i = 128; i < 132; i++) dicm += String.fromCharCode(v.getUint8(i));
            if (dicm === "DICM") return "dicom";
            if (v.getInt32(0, true) === 348 || v.getInt32(0, false) === 348) return "nifti";
        }
        return null;
    }

    function isGzip(buffer) {
        if (!buffer || buffer.byteLength < 2) return false;
        const v = new DataView(buffer);
        return v.getUint8(0) === 0x1f && v.getUint8(1) === 0x8b;
    }

    async function gunzip(buffer) {
        if (typeof DecompressionStream === "undefined") {
            throw new Error("this browser cannot decompress gzip (DecompressionStream unavailable)");
        }
        const ds = new DecompressionStream("gzip");
        const stream = new Blob([buffer]).stream().pipeThrough(ds);
        return await new Response(stream).arrayBuffer();
    }

    // ------------------------------------------------ clinical annotation --

    /**
     * Clinical history carried in a format's free-text field.
     *
     * STL, NIfTI and Fluent have no clinical vocabulary — DICOM does, which is
     * why it needs none of this. But each has one designated free-text slot:
     *
     *   STL      the 80-byte binary header, conventionally a comment and
     *            ignored by every geometry reader
     *   NIfTI    `descrip` at offset 148, a standard 80-byte description field
     *   Fluent   a `(0 "…")` comment section
     *
     * A compact line is written there and read back:
     *
     *   NEUROFLOW/1 A=64 H=1 S=0 P=Other L=MCA
     *
     * This is a CONVENTION, not a standard. A file from elsewhere will not have
     * it, and those still report PHASES as unscored rather than inventing the
     * history — which is the behaviour that matters. It exists so that a
     * pipeline exporting a surface can carry the case's history with it instead
     * of losing it at the format boundary.
     */
    const ANNOT_RE = /NEUROFLOW\/1\s+([^\r\n\0]*)/;

    function parseAnnotation(text) {
        if (!text) return null;
        const m = ANNOT_RE.exec(String(text));
        if (!m) return null;

        const kv = {};
        for (const tok of m[1].trim().split(/\s+/)) {
            const i = tok.indexOf("=");
            // Trim the delimiters a container may append: inside a Fluent
            // comment the last token arrives as `L=MCA")`, which would
            // otherwise fail the site whitelist and silently drop it.
            if (i > 0) {
                kv[tok.slice(0, i).toUpperCase()] =
                    tok.slice(i + 1).replace(/["')\]\s]+$/, "");
            }
        }
        // A key that is absent stays null. "0" and "missing" are different
        // answers, and collapsing them is what scores an unknown as a negative.
        const bool = (v) => (v === undefined ? null : v === "1" || /^(y|yes|true)$/i.test(v));
        const age = kv.A !== undefined ? parseInt(kv.A, 10) : null;
        const site = kv.L ? kv.L.toUpperCase() : null;
        return {
            age: Number.isFinite(age) ? age : null,
            hypertension: bool(kv.H),
            earlierSAH: bool(kv.S),
            population: kv.P || null,
            site: ["ICA", "MCA", "ACOM_PCOM_POST"].includes(site) ? site : null,
            source: "file annotation",
        };
    }

    // ---------------------------------------------------------------- STL --

    function readStl(buffer) {
        const view = new DataView(buffer);
        const n = buffer.byteLength;

        // Binary STL declares its triangle count at byte 80; the file must then
        // be exactly 84 + 50*count bytes. Checking that is the only reliable
        // way to tell binary from ASCII, because a binary file's 80-byte header
        // can legitimately begin with the word "solid".
        let binary = false;
        if (n >= 84) {
            const tris = view.getUint32(80, true);
            binary = (84 + tris * 50) === n;
        }

        // The 80-byte header carries the annotation when there is one.
        let headerText = "";
        for (let i = 0; i < Math.min(80, n); i++) {
            const ch = view.getUint8(i);
            if (ch >= 32 && ch < 127) headerText += String.fromCharCode(ch);
        }

        const pts = [];
        if (binary) {
            const tris = view.getUint32(80, true);
            for (let i = 0; i < tris; i++) {
                const o = 84 + i * 50 + 12;          // skip the facet normal
                for (let k = 0; k < 3; k++) {
                    pts.push([
                        view.getFloat32(o + k * 12, true),
                        view.getFloat32(o + k * 12 + 4, true),
                        view.getFloat32(o + k * 12 + 8, true),
                    ]);
                }
            }
        } else {
            const text = new TextDecoder().decode(buffer);
            const re = /vertex\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)/g;
            let m;
            while ((m = re.exec(text)) !== null) {
                pts.push([parseFloat(m[1]), parseFloat(m[2]), parseFloat(m[3])]);
            }
        }

        if (pts.length < 12) {
            return { ok: false, reason: "no triangles found — the file may be truncated or not STL" };
        }
        return { ok: true, points: pts, binary, triangles: pts.length / 3, headerText };
    }

    // -------------------------------------------------------------- Fluent --

    function readFluentNodes(buffer) {
        // Fluent case files are sectioned as `(index (args) (payload))`. Only
        // section 10 — the node coordinates — is read here. That is enough for
        // geometry and is NOT a general parser for the format: zones, faces,
        // cells and boundary conditions are all skipped.
        const text = new TextDecoder("utf-8", { fatal: false }).decode(buffer);
        const pts = [];
        // (0 "…") comment sections, where an annotation would live.
        const comments = (text.match(/\(\s*0\s+"([^"]*)"/g) || []).join(" ");

        const re = /\(\s*10\s*\(([^)]*)\)\s*\(/g;
        let m;
        while ((m = re.exec(text)) !== null) {
            const header = m[1].trim().split(/\s+/);
            // A zero zone-id is the declaration of the total node count, not a
            // block of coordinates.
            if (header[0] === "0" || header[0] === "(0") continue;

            let i = re.lastIndex;
            let depth = 1;
            while (i < text.length && depth > 0) {
                if (text[i] === "(") depth++;
                else if (text[i] === ")") depth--;
                if (depth === 0) break;
                i++;
            }
            const body = text.slice(re.lastIndex, i);
            const nums = body.trim().split(/\s+/).map(Number).filter(Number.isFinite);
            for (let k = 0; k + 2 < nums.length; k += 3) {
                pts.push([nums[k], nums[k + 1], nums[k + 2]]);
            }
        }

        if (pts.length < 12) {
            return {
                ok: false,
                reason: "no node-coordinate section (10 …) found. Binary Fluent cases and "
                      + "some writer versions are not supported.",
            };
        }
        return { ok: true, points: pts, nodes: pts.length, comments };
    }

    // --------------------------------------------------------------- NIfTI --

    function readNifti(buffer) {
        const v = new DataView(buffer);
        // The header records its own size as 348; if that reads back wrong the
        // file is big-endian and every subsequent field must be too.
        let little = true;
        if (v.getInt32(0, true) !== 348) {
            if (v.getInt32(0, false) === 348) little = false;
            else return { ok: false, reason: "not a NIfTI-1 file (header size is not 348)" };
        }

        const dim = [];
        for (let i = 0; i < 8; i++) dim.push(v.getInt16(40 + i * 2, little));
        const datatype = v.getInt16(70, little);
        const pixdim = [];
        for (let i = 0; i < 8; i++) pixdim.push(v.getFloat32(76 + i * 4, little));
        const voxOffset = Math.max(352, Math.round(v.getFloat32(108, little)));

        const [nx, ny, nz] = [dim[1] || 0, dim[2] || 0, dim[3] || 1];
        if (nx < 2 || ny < 2) return { ok: false, reason: "NIfTI has no usable image dimensions" };

        const READERS = {
            2:  (o) => v.getUint8(o),                  // uint8
            4:  (o) => v.getInt16(o, little),          // int16
            8:  (o) => v.getInt32(o, little),          // int32
            16: (o) => v.getFloat32(o, little),        // float32
            64: (o) => v.getFloat64(o, little),        // float64
            256:(o) => v.getInt8(o),                   // int8
            512:(o) => v.getUint16(o, little),         // uint16
        };
        const BYTES = { 2: 1, 4: 2, 8: 4, 16: 4, 64: 8, 256: 1, 512: 2 };
        const read = READERS[datatype];
        if (!read) {
            return { ok: false, reason: `unsupported NIfTI datatype code ${datatype}` };
        }
        const bpv = BYTES[datatype];

        // Take the middle axial slice, which is where a mid-stack sac sits —
        // the same choice the DICOM path makes.
        const k = Math.floor(nz / 2);
        const slice = new Float64Array(nx * ny);
        for (let j = 0; j < ny; j++) {
            for (let i = 0; i < nx; i++) {
                const idx = i + j * nx + k * nx * ny;
                const off = voxOffset + idx * bpv;
                if (off + bpv > buffer.byteLength) {
                    return { ok: false, reason: "NIfTI voxel data is truncated" };
                }
                slice[i + j * nx] = read(off);
            }
        }

        // `descrip` — 80 bytes at offset 148, the standard short-description
        // field. Read as text so an annotation written there survives.
        let descrip = "";
        for (let i = 148; i < 228 && i < buffer.byteLength; i++) {
            const ch = v.getUint8(i);
            if (ch === 0) break;
            if (ch >= 32 && ch < 127) descrip += String.fromCharCode(ch);
        }

        return {
            ok: true, descrip,
            slice, rows: ny, cols: nx,
            spacing: [pixdim[2] || 1, pixdim[1] || 1],     // [row, col] mm
            dims: [nx, ny, nz],
            datatype,
        };
    }

    // --------------------------------------- measuring a sac from a surface --

    /**
     * Dome and neck from a triangulated surface, by MAXIMUM INSCRIBED SPHERE.
     *
     * WHY THE PREVIOUS METHOD FAILED
     * It assumed the model was one straight tube with one sphere on it — which
     * my phantoms are, and which a real scan is not. It fitted a single axis,
     * measured every vertex radially from it, and called the outliers "sac".
     * On a genuine arterial tree, with branches running in many directions at
     * many calibres, that measures the bounding box: an uploaded cerebral
     * artery model reported a 176.7 mm dome. No aneurysm is that size — that
     * is the width of the head.
     *
     * WHAT THIS DOES INSTEAD
     * An aneurysm is a local DILATION: the vessel is wider there than anywhere
     * else. So the largest sphere that fits inside the lumen sits in the sac,
     * and its diameter is the dome diameter. This is the maximum inscribed
     * sphere — the quantity vessel-morphology tools derive from the medial
     * axis — and it assumes nothing about how the vessel is laid out.
     *
     *   1. voxelise the surface onto a grid
     *   2. flood-fill from a corner to mark the exterior
     *   3. interior = neither surface nor exterior
     *   4. distance transform over the interior
     *   5. dome   = 2 x the largest distance
     *      parent = 2 x the ordinary calibre of the rest of the lumen
     *      neck   = the constriction between the two
     *
     * Grid resolution is capped so an arbitrarily large model cannot allocate
     * an arbitrarily large grid, and the voxel size is reported so the
     * measurement's granularity is visible rather than implied.
     */
    function measureFromPoints(points, unitScale) {
        const n = points.length;
        if (n < 12) return { ok: false, reason: "too few points to measure" };

        const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
        for (const p of points) {
            for (let d = 0; d < 3; d++) {
                if (p[d] < lo[d]) lo[d] = p[d];
                if (p[d] > hi[d]) hi[d] = p[d];
            }
        }
        const span = [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]];
        const longest = Math.max(span[0], span[1], span[2]);
        if (!(longest > 0)) return { ok: false, reason: "degenerate geometry" };

        // Resolution is set by the FEATURE, not the bounding box.
        //
        // A cerebral artery model is ~100 mm long and ~4 mm across. At 160
        // voxels along the longest axis the lumen is barely 6 voxels wide, the
        // one-voxel shell consumes most of that, and the inscribed radius comes
        // out ~37% low — a 4.0 mm vessel measured 2.5 mm. The grid has to
        // resolve the vessel, not the extent.
        //
        // 0.2 mm targets ~20 voxels across a 4 mm artery. The cap keeps memory
        // bounded for a large model, at the cost of accuracy the caller can see
        // in `voxelSizeMm`.
        const TARGET_MM = 0.2;
        const MAX_VOXELS = 24e6;
        let h = TARGET_MM / unitScale;               // model units
        const est = () => (Math.ceil(span[0] / h) + 3) * (Math.ceil(span[1] / h) + 3)
                        * (Math.ceil(span[2] / h) + 3);
        while (est() > MAX_VOXELS) h *= 1.25;
        if (h > longest / 8) h = longest / 8;         // never coarser than 8 cells
        const nx = Math.max(3, Math.ceil(span[0] / h) + 3);
        const ny = Math.max(3, Math.ceil(span[1] / h) + 3);
        const nz = Math.max(3, Math.ceil(span[2] / h) + 3);
        const total = nx * ny * nz;
        if (total > MAX_VOXELS * 1.2) return { ok: false, reason: "model too large to voxelise" };

        const idx = (i, j, k) => i + nx * (j + ny * k);
        const SURF = 1, OUT = 2;
        const grid = new Uint8Array(total);

        // 1. Mark the shell. Vertices alone suffice: a scan-derived surface is
        //    finely triangulated relative to this grid, so the shell closes.
        for (const p of points) {
            const i = Math.min(nx - 1, Math.max(0, Math.round((p[0] - lo[0]) / h) + 1));
            const j = Math.min(ny - 1, Math.max(0, Math.round((p[1] - lo[1]) / h) + 1));
            const k = Math.min(nz - 1, Math.max(0, Math.round((p[2] - lo[2]) / h) + 1));
            grid[idx(i, j, k)] = SURF;
        }

        // 2. Flood-fill the exterior from the padded corner.
        const stack = new Int32Array(total);
        let sp = 0;
        stack[sp++] = idx(0, 0, 0);
        grid[idx(0, 0, 0)] = OUT;
        while (sp > 0) {
            const cur = stack[--sp];
            const k = (cur / (nx * ny)) | 0;
            const rem = cur - k * nx * ny;
            const j = (rem / nx) | 0;
            const i = rem - j * nx;
            const push = (a, b, c) => {
                if (a < 0 || b < 0 || c < 0 || a >= nx || b >= ny || c >= nz) return;
                const t = idx(a, b, c);
                if (grid[t] === 0) { grid[t] = OUT; stack[sp++] = t; }
            };
            push(i - 1, j, k); push(i + 1, j, k);
            push(i, j - 1, k); push(i, j + 1, k);
            push(i, j, k - 1); push(i, j, k + 1);
        }

        // 3+4. Chamfer distance transform over the interior, in voxels. Two
        //      sweeps approximate Euclidean distance closely enough for a
        //      radius at a fraction of the cost of an exact transform.
        const INF = 1e9;
        const dist = new Float32Array(total);
        let interior = 0;
        for (let t = 0; t < total; t++) {
            if (grid[t] === 0) { dist[t] = INF; interior++; } else dist[t] = 0;
        }
        if (interior < 20) {
            return { ok: false, reason: "no enclosed lumen found — the surface may be open" };
        }

        // (1, sqrt2, sqrt3) chamfer. Omitting the body diagonal underestimates
        // Euclidean distance by up to ~13% in 3D — measured directly as a
        // 6.55 mm dome on a sphere known to be 7.5 mm.
        const D1 = 1, D2 = Math.SQRT2, D3 = Math.sqrt(3);
        const relax = (t, u, w) => { const d = dist[u] + w; if (d < dist[t]) dist[t] = d; };
        for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
            const t = idx(i, j, k);
            if (dist[t] === 0) continue;
            if (i > 0) relax(t, idx(i - 1, j, k), D1);
            if (j > 0) relax(t, idx(i, j - 1, k), D1);
            if (k > 0) relax(t, idx(i, j, k - 1), D1);
            if (i > 0 && j > 0) relax(t, idx(i - 1, j - 1, k), D2);
            if (i > 0 && k > 0) relax(t, idx(i - 1, j, k - 1), D2);
            if (j > 0 && k > 0) relax(t, idx(i, j - 1, k - 1), D2);
            if (i > 0 && j > 0 && k > 0) relax(t, idx(i - 1, j - 1, k - 1), D3);
        }
        for (let k = nz - 1; k >= 0; k--) for (let j = ny - 1; j >= 0; j--) for (let i = nx - 1; i >= 0; i--) {
            const t = idx(i, j, k);
            if (dist[t] === 0) continue;
            if (i < nx - 1) relax(t, idx(i + 1, j, k), D1);
            if (j < ny - 1) relax(t, idx(i, j + 1, k), D1);
            if (k < nz - 1) relax(t, idx(i, j, k + 1), D1);
            if (i < nx - 1 && j < ny - 1) relax(t, idx(i + 1, j + 1, k), D2);
            if (i < nx - 1 && k < nz - 1) relax(t, idx(i + 1, j, k + 1), D2);
            if (j < ny - 1 && k < nz - 1) relax(t, idx(i, j + 1, k + 1), D2);
            if (i < nx - 1 && j < ny - 1 && k < nz - 1) relax(t, idx(i + 1, j + 1, k + 1), D3);
        }

        // 5. Radii, converted to millimetres.
        const radii = [];
        let maxR = 0;
        for (let t = 0; t < total; t++) {
            if (grid[t] !== 0) continue;
            const d = dist[t];
            if (d >= INF) continue;
            radii.push(d);
            if (d > maxR) maxR = d;
        }
        if (!radii.length) return { ok: false, reason: "no interior voxels" };
        radii.sort((a, b) => a - b);

        const toMm = h * unitScale;
        const domeMm = 2 * maxR * toMm;

        // Parent calibre from voxels ON the medial axis — those that are local
        // maxima of the distance field, i.e. no neighbour is further from the
        // wall. In a tube of radius R only the centreline reaches R; every
        // other interior voxel is nearer the wall, so a percentile over ALL of
        // them measures how the lumen is filled rather than how wide it is.
        // That reported 2.5 mm for a vessel known to be 4.0 mm across.
        const ridge = [];
        for (let k = 1; k < nz - 1; k++) for (let j = 1; j < ny - 1; j++) for (let i = 1; i < nx - 1; i++) {
            const t = idx(i, j, k);
            if (grid[t] !== 0) continue;
            const d = dist[t];
            if (d >= INF || d < 1) continue;
            if (d >= dist[idx(i - 1, j, k)] && d >= dist[idx(i + 1, j, k)] &&
                d >= dist[idx(i, j - 1, k)] && d >= dist[idx(i, j + 1, k)] &&
                d >= dist[idx(i, j, k - 1)] && d >= dist[idx(i, j, k + 1)]) {
                ridge.push(d);
            }
        }
        ridge.sort((a, b) => a - b);
        // 75th percentile of the medial axis, not the median. Ridge points
        // cluster wherever the vessel tapers — near caps, bifurcations and the
        // sac junction — and those thin points outnumber the full-calibre run,
        // pulling a median 30% below the true diameter. The sac's own ridge
        // points are few enough not to dominate p75.
        const parentMm = ridge.length
            ? 2 * ridge[Math.floor(ridge.length * 0.75)] * toMm
            : 2 * radii[Math.floor(radii.length * 0.95)] * toMm;

        // A dilation only counts as a sac if it is meaningfully wider than the
        // vessel carrying it. Otherwise this is a healthy tree, and saying so
        // beats reporting its widest point as an aneurysm.
        const ratio = parentMm > 0 ? domeMm / parentMm : 0;

        const between = radii.filter((r) => {
            const d = 2 * r * toMm;
            return d > parentMm && d < domeMm;
        });
        const neckRaw = between.length
            ? 2 * between[Math.floor(between.length * 0.25)] * toMm
            : Math.max(parentMm, domeMm * 0.6);
        const neckMm = Math.min(neckRaw, domeMm);

        return {
            ok: true,
            bulgeDetected: ratio >= 1.4,
            domeDiameterMm: +domeMm.toFixed(2),
            neckDiameterMm: +neckMm.toFixed(2),
            parentDiameterMm: +parentMm.toFixed(2),
            aspectRatio: +(domeMm / Math.max(neckMm, 0.1)).toFixed(2),
            dilationRatio: +ratio.toFixed(2),
            voxelSizeMm: +toMm.toFixed(3),
            interiorVoxels: interior,
            method: "maximum inscribed sphere on a voxelised lumen",
            caveat: "Measured from the supplied surface, no thresholding. The dome is "
                  + "the largest sphere fitting inside the lumen. The neck is "
                  + "approximate and the sac is not separated from the parent vessel "
                  + "anatomically — confirm before relying on it.",
        };
    }

    /**
     * STL and Fluent files carry no units. Millimetres and metres are both
     * common, so the scale is inferred from the model's own extent: a cerebral
     * artery segment is centimetres long, which is ~0.1 in metres and ~100 in
     * millimetres. Guessing wrong changes every result by 1000x, so the choice
     * is reported rather than assumed silently.
     */
    function inferUnitScale(points) {
        const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
        for (const p of points) {
            for (let d = 0; d < 3; d++) {
                if (p[d] < lo[d]) lo[d] = p[d];
                if (p[d] > hi[d]) hi[d] = p[d];
            }
        }
        const extent = Math.max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]);
        if (extent < 1) return { scale: 1000, units: "m", extent };      // metres
        if (extent < 100) return { scale: 10, units: "cm", extent };     // centimetres
        return { scale: 1, units: "mm", extent };                        // millimetres
    }

    // ------------------------------------------------------------- entry ---

    /**
     * Read any supported file and return a sac measurement.
     *
     * @returns {Promise<{ok, format, label, measurement?, meta?, clinical?, reason?}>}
     */
    async function read(fileName, buffer) {
        let buf = buffer;
        let wasGzipped = false;
        if (isGzip(buf)) {
            try { buf = await gunzip(buf); wasGzipped = true; }
            catch (e) { return { ok: false, reason: `could not decompress: ${e.message}` }; }
        }

        const format = detect(fileName, buf);
        if (!format) {
            return {
                ok: false,
                reason: "Unrecognised file type. Supported: DICOM (.dcm), NIfTI (.nii/.nii.gz), "
                      + "STL (.stl) and Fluent case (.cas/.cas.gz).",
            };
        }
        const label = FORMATS[format].label + (wasGzipped ? " (gzip)" : "");

        if (format === "dicom") {
            const parsed = global.NeuroDicom.parse(buf);
            if (!parsed.ok) return { ok: false, format, label, reason: parsed.reason };
            return {
                ok: true, format, label,
                measurement: global.NeuroDicom.measureSac(buf, parsed.tags),
                meta: parsed.tags,
                clinical: global.NeuroDicom.clinicalHistory(parsed.tags),
            };
        }

        if (format === "nifti") {
            const nii = readNifti(buf);
            if (!nii.ok) return { ok: false, format, label, reason: nii.reason };
            // Reuse the DICOM measurement: identical problem, identical method.
            const m = global.NeuroDicom.measureSlice(
                nii.slice, nii.rows, nii.cols, nii.spacing[0], nii.spacing[1]);
            const annot = parseAnnotation(nii.descrip);
            return {
                ok: true, format, label, measurement: m,
                meta: {
                    modality: "MR", rows: nii.rows, columns: nii.cols,
                    sliceThickness: nii.spacing[0],
                    seriesDescription: `NIfTI volume ${nii.dims.join("x")}`,
                },
                clinical: annot || {},   // from `descrip`, when annotated
            };
        }

        const geom = format === "stl" ? readStl(buf) : readFluentNodes(buf);
        if (!geom.ok) return { ok: false, format, label, reason: geom.reason };

        const annot = parseAnnotation(geom.headerText || geom.comments || "");
        const units = inferUnitScale(geom.points);
        const m = measureFromPoints(geom.points, units.scale);
        if (m.ok) {
            m.unitsAssumed = units.units;
            m.method = `${m.method}; units inferred as ${units.units} `
                     + `from a ${units.extent.toFixed(2)} ${units.units} extent`;
        }
        return {
            ok: true, format, label, measurement: m,
            meta: {
                modality: format === "stl" ? "SURFACE" : "MESH",
                seriesDescription: format === "stl"
                    ? `STL surface, ${geom.triangles} triangles`
                    : `Fluent case, ${geom.nodes} nodes`,
                unitsAssumed: units.units,
            },
            clinical: annot || {},     // from the header/comment, when annotated
        };
    }

    global.NeuroReaders = {
        read, detect, isGzip, readStl, readNifti, readFluentNodes, parseAnnotation,
        measureFromPoints, inferUnitScale, FORMATS,
    };
})(typeof window !== "undefined" ? window : globalThis);
