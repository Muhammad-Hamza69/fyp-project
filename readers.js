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
     * Dome and neck from a point cloud (STL vertices or Fluent nodes).
     *
     * The vessel is a tube with a sac on it, so the parent artery defines a
     * dominant axis. Points are measured radially from that axis: the parent
     * wall sits at a roughly constant radius, and the sac is everything
     * significantly beyond it.
     *
     * The parent radius is taken at the 35th percentile, not the median.
     * A median is pulled upward by the sac's own points and over-estimates the
     * parent — which then makes the sac look smaller than it is. That exact
     * mistake, made in the Python pipeline, put a sac's TAWSS out by a factor
     * of 22,000 before it was caught.
     */
    function measureFromPoints(points, unitScale) {
        const n = points.length;
        if (n < 12) return { ok: false, reason: "too few points to measure" };

        const c = [0, 0, 0];
        for (const p of points) { c[0] += p[0]; c[1] += p[1]; c[2] += p[2]; }
        c[0] /= n; c[1] /= n; c[2] /= n;

        // Longest bounding-box side is the vessel axis. Robust here because the
        // parent artery is far longer than the sac is wide.
        const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
        for (const p of points) {
            for (let d = 0; d < 3; d++) {
                if (p[d] < lo[d]) lo[d] = p[d];
                if (p[d] > hi[d]) hi[d] = p[d];
            }
        }
        const span = [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]];
        const axis = span.indexOf(Math.max(...span));
        const r1 = (axis + 1) % 3, r2 = (axis + 2) % 3;

        const radial = points.map((p) => Math.hypot(p[r1] - c[r1], p[r2] - c[r2]));
        const sorted = [...radial].sort((a, b) => a - b);
        const parentR = sorted[Math.floor(sorted.length * 0.35)];

        // Everything well outside the parent wall is sac.
        const sac = [];
        for (let i = 0; i < n; i++) {
            if (radial[i] > parentR * 1.5) sac.push(points[i]);
        }
        if (sac.length < 8) {
            return {
                ok: true, bulgeDetected: false,
                parentDiameterMm: parentR * 2 * unitScale,
                domeDiameterMm: 0, neckDiameterMm: 0,
            };
        }

        const slo = [Infinity, Infinity, Infinity], shi = [-Infinity, -Infinity, -Infinity];
        for (const p of sac) {
            for (let d = 0; d < 3; d++) {
                if (p[d] < slo[d]) slo[d] = p[d];
                if (p[d] > shi[d]) shi[d] = p[d];
            }
        }
        const sacSpan = [shi[0] - slo[0], shi[1] - slo[1], shi[2] - slo[2]];
        const domeMm = Math.max(...sacSpan) * unitScale;

        // Neck: the sac's width where it meets the parent wall, taken as its
        // extent along the vessel axis at the innermost band of sac points.
        const inner = sac.filter((p) => {
            const rr = Math.hypot(p[r1] - c[r1], p[r2] - c[r2]);
            return rr < parentR * 2.0;
        });
        let neckMm = domeMm * 0.75;
        if (inner.length >= 4) {
            let a = Infinity, b = -Infinity;
            for (const p of inner) { if (p[axis] < a) a = p[axis]; if (p[axis] > b) b = p[axis]; }
            neckMm = Math.min((b - a) * unitScale, domeMm);
        }

        return {
            ok: true,
            bulgeDetected: true,
            domeDiameterMm: +domeMm.toFixed(2),
            neckDiameterMm: +Math.max(neckMm, 0.1).toFixed(2),
            parentDiameterMm: +(parentR * 2 * unitScale).toFixed(2),
            aspectRatio: +(domeMm / Math.max(neckMm, 0.1)).toFixed(2),
            sacPoints: sac.length,
            method: "radial profile about the vessel axis (parent radius at p35)",
            caveat: "Measured from the supplied surface. No thresholding is involved, "
                  + "so this is more direct than the image-based route — but the file "
                  + "must contain the vessel and the sac and nothing else.",
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
