/**
 * Minimal, real DICOM parser — binary, in the browser, no dependencies.
 *
 * WHAT THIS REPLACES
 * The upload handler read the dropped file with `FileReader.readAsText()` and
 * pulled "metadata" out of it with regexes like /PATIENT_ID\s*=\s*(PT-\d{4}-\d{4})/.
 * That is not DICOM parsing. It worked only because the three .dcm files in the
 * repo were ASCII stubs — plain text that happened to carry that extension.
 * pydicom rejects all three, and none has the DICM magic at byte 128. A real
 * scan dropped on that handler would have produced mojibake and then silently
 * fallen back to hardcoded defaults (512x512, 142 slices, 0.5 mm), presenting
 * invented numbers as though they had been read from the file.
 *
 * This reads the actual byte stream: the 128-byte preamble, the DICM marker,
 * the File Meta group, the transfer syntax, and then the data set — handling
 * both Explicit and Implicit VR Little Endian, which between them cover what
 * clinical scanners emit and what pydicom writes.
 *
 * Compressed transfer syntaxes (JPEG, JPEG-LS, JPEG 2000, RLE) are detected and
 * reported rather than misparsed: the header is still readable, only the pixel
 * data is encoded, and this parser does not claim to decode pixels.
 */

(function (global) {
    "use strict";

    // Tags worth surfacing. Everything else is skipped rather than stored —
    // a scan carries hundreds of elements and the dashboard needs a dozen.
    const TAGS = {
        "0002,0010": "transferSyntaxUID",
        "0008,0020": "studyDate",
        "0008,0030": "studyTime",
        "0008,0060": "modality",
        "0008,0070": "manufacturer",
        "0008,1030": "studyDescription",
        "0008,103E": "seriesDescription",
        "0008,1090": "manufacturerModelName",
        "0010,0010": "patientName",
        "0010,0020": "patientID",
        "0010,0040": "patientSex",
        "0010,1010": "patientAge",
        // Clinical history for the PHASES score. These are the standard places
        // a scan carries it — none of it is derivable from the pixels, and
        // defaulting it instead would score unknowns as zero.
        "0010,2160": "ethnicGroup",                 // population term
        "0010,21B0": "additionalPatientHistory",    // hypertension / prior SAH
        "0008,1080": "admittingDiagnoses",          // names the parent vessel
        "0018,0015": "bodyPartExamined",
        "0018,0050": "sliceThickness",
        "0018,0088": "spacingBetweenSlices",
        "0018,1030": "protocolName",
        "0018,0020": "scanningSequence",
        "0018,0023": "mrAcquisitionType",
        "0020,000D": "studyInstanceUID",
        "0020,000E": "seriesInstanceUID",
        "0020,0013": "instanceNumber",
        "0028,0010": "rows",
        "0028,0011": "columns",
        "0028,0030": "pixelSpacing",
        "0028,0100": "bitsAllocated",
        "0028,0101": "bitsStored",
        "0028,0004": "photometricInterpretation",
    };

    // VRs whose length field is 4 bytes preceded by 2 reserved bytes. Getting
    // this wrong desynchronises the whole stream, and the failure looks like
    // garbage tag values rather than an error.
    const VR_32BIT = new Set(["OB", "OW", "OF", "OD", "OL", "SQ", "UT", "UN", "UC", "UR"]);
    const VR_NUMERIC = new Set(["US", "UL", "SS", "SL", "FL", "FD"]);

    const COMPRESSED = {
        "1.2.840.10008.1.2.4.50": "JPEG Baseline",
        "1.2.840.10008.1.2.4.51": "JPEG Extended",
        "1.2.840.10008.1.2.4.57": "JPEG Lossless",
        "1.2.840.10008.1.2.4.70": "JPEG Lossless SV1",
        "1.2.840.10008.1.2.4.80": "JPEG-LS Lossless",
        "1.2.840.10008.1.2.4.81": "JPEG-LS Lossy",
        "1.2.840.10008.1.2.4.90": "JPEG 2000 Lossless",
        "1.2.840.10008.1.2.4.91": "JPEG 2000",
        "1.2.840.10008.1.2.5": "RLE Lossless",
    };

    const IMPLICIT_VR_LE = "1.2.840.10008.1.2";
    const EXPLICIT_VR_BE = "1.2.840.10008.1.2.2";

    function tagKey(group, element) {
        const h = (n) => n.toString(16).toUpperCase().padStart(4, "0");
        return `${h(group)},${h(element)}`;
    }

    function readString(view, offset, length) {
        let s = "";
        for (let i = 0; i < length; i++) {
            const c = view.getUint8(offset + i);
            if (c === 0) break;                     // DICOM pads with NUL
            s += String.fromCharCode(c);
        }
        return s.trim();
    }

    function readNumeric(view, offset, length, vr, little) {
        switch (vr) {
            case "US": return length >= 2 ? view.getUint16(offset, little) : null;
            case "UL": return length >= 4 ? view.getUint32(offset, little) : null;
            case "SS": return length >= 2 ? view.getInt16(offset, little) : null;
            case "SL": return length >= 4 ? view.getInt32(offset, little) : null;
            case "FL": return length >= 4 ? view.getFloat32(offset, little) : null;
            case "FD": return length >= 8 ? view.getFloat64(offset, little) : null;
            default: return null;
        }
    }

    /**
     * Parse a DICOM part-10 file.
     *
     * @param {ArrayBuffer} buffer
     * @returns {{ok: boolean, reason?: string, tags?: object, warnings?: string[]}}
     */
    function parse(buffer) {
        const warnings = [];
        if (!buffer || buffer.byteLength < 132) {
            return { ok: false, reason: "File is too small to be a DICOM part-10 file (needs at least 132 bytes)." };
        }

        const view = new DataView(buffer);

        // The DICM marker sits after a 128-byte preamble. Its absence is the
        // single clearest signal that a file is not DICOM, and is exactly what
        // distinguishes a real scan from the ASCII stubs this used to accept.
        if (readString(view, 128, 4) !== "DICM") {
            return {
                ok: false,
                reason: "Not a DICOM file — the 'DICM' marker is missing at byte 128. "
                      + "A file named .dcm is not necessarily DICOM.",
            };
        }

        const tags = {};
        let offset = 132;
        // The File Meta group (0002) is ALWAYS Explicit VR Little Endian,
        // whatever the data set that follows it uses.
        let explicit = true;
        let little = true;
        let inFileMeta = true;
        let fileMetaEnd = Infinity;

        while (offset + 8 <= view.byteLength) {
            const group = view.getUint16(offset, little);
            const element = view.getUint16(offset + 2, little);
            offset += 4;

            // Switch to the data set's own encoding once the File Meta group ends.
            if (inFileMeta && group !== 0x0002) {
                inFileMeta = false;
                const ts = tags.transferSyntaxUID;
                if (ts === IMPLICIT_VR_LE) explicit = false;
                else if (ts === EXPLICIT_VR_BE) little = false;
            }

            let vr = null;
            let length;

            if (explicit) {
                vr = readString(view, offset, 2);
                offset += 2;
                if (VR_32BIT.has(vr)) {
                    offset += 2;                     // reserved
                    length = view.getUint32(offset, little);
                    offset += 4;
                } else {
                    length = view.getUint16(offset, little);
                    offset += 2;
                }
            } else {
                length = view.getUint32(offset, little);
                offset += 4;
            }

            const key = tagKey(group, element);

            // Undefined length marks a sequence or encapsulated pixel data.
            // Nothing beyond this point is needed, and walking it correctly
            // requires item-delimiter handling this parser does not do — so it
            // stops rather than guessing and emitting nonsense.
            if (length === 0xFFFFFFFF) {
                warnings.push(`Stopped at ${key}: undefined-length element (sequence or encapsulated pixel data).`);
                break;
            }

            // Pixel data is the end of anything header-related, and can be
            // megabytes; there is no reason to walk into it.
            if (group === 0x7FE0 && element === 0x0010) {
                tags.pixelDataLength = length;
                // Keep the offset so the pixels can be measured. Without this
                // the only geometry available is whatever a user types, and the
                // dome diameter — the quantity the hemodynamics actually turn
                // on — is not in the header at all. It is in here.
                tags.pixelDataOffset = offset;
                break;
            }

            if (offset + length > view.byteLength) {
                warnings.push(`Truncated at ${key}: element claims ${length} bytes but the file ends first.`);
                break;
            }

            const name = TAGS[key];
            if (name) {
                let value;
                if (vr && VR_NUMERIC.has(vr)) {
                    value = readNumeric(view, offset, length, vr, little);
                } else {
                    value = readString(view, offset, length);
                    // Implicit VR carries no type, so numeric-looking values
                    // arrive as text; convert the ones the UI treats as numbers.
                    if (!explicit && /^-?\d+(\.\d+)?$/.test(value)) value = parseFloat(value);
                }
                tags[name] = value;
            }

            offset += length;
            if (length % 2 === 1) offset += 1;       // elements are even-padded
        }

        if (!tags.transferSyntaxUID) {
            warnings.push("No transfer syntax in the File Meta group; assumed Explicit VR Little Endian.");
        }
        const compressed = COMPRESSED[tags.transferSyntaxUID];
        if (compressed) {
            warnings.push(`Pixel data is ${compressed}-compressed. The header is read; pixels are not decoded.`);
        }

        // A DICOM file with no modality and no dimensions parsed is a sign the
        // stream desynchronised, even though nothing threw.
        if (!tags.modality && !tags.rows && !tags.patientID) {
            return {
                ok: false,
                reason: "DICM marker found, but no readable header elements — the file may be corrupt "
                      + "or use an unsupported encoding.",
                warnings,
            };
        }

        return { ok: true, tags, warnings, compressed: compressed || null };
    }

    /**
     * Read the pixel array as a Float64Array of raw stored values.
     *
     * Handles the uncompressed little-endian cases a scanner actually emits:
     * 8- and 16-bit, signed or unsigned. Compressed transfer syntaxes are
     * refused rather than reinterpreted as raw — decoding JPEG 2000 is not
     * something this file pretends to do.
     */
    function pixels(buffer, tags) {
        if (!tags || tags.pixelDataOffset == null) return null;
        if (COMPRESSED[tags.transferSyntaxUID]) return null;

        const rows = Number(tags.rows), cols = Number(tags.columns);
        const bits = Number(tags.bitsAllocated) || 16;
        if (!rows || !cols) return null;

        const view = new DataView(buffer);
        const n = rows * cols;
        const signed = Number(tags.pixelRepresentation) === 1;
        const out = new Float64Array(n);
        const little = tags.transferSyntaxUID !== EXPLICIT_VR_BE;

        for (let i = 0; i < n; i++) {
            const o = tags.pixelDataOffset + i * (bits === 8 ? 1 : 2);
            if (o + (bits === 8 ? 1 : 2) > view.byteLength) return null;
            out[i] = bits === 8
                ? (signed ? view.getInt8(o) : view.getUint8(o))
                : (signed ? view.getInt16(o, little) : view.getUint16(o, little));
        }
        return { data: out, rows, cols };
    }

    /**
     * Measure the aneurysm sac from the image, in millimetres.
     *
     * WHY THIS EXISTS
     * Dome and neck diameter drive every hemodynamic estimate, and neither is
     * in the DICOM header — they are properties of the pixels. Without this the
     * user has to type them, and the results then describe what was typed
     * rather than what was scanned.
     *
     * WHAT IT DOES
     * TOF-MRA is a bright-vessel modality: flowing blood is high signal against
     * suppressed background. So the lumen separates on intensity alone. The
     * threshold is chosen by Otsu — computed from the histogram rather than
     * hard-coded, so it adapts to each scan's own contrast. The largest
     * connected bright region is taken as the vasculature, its width profiled
     * column by column, and the sac identified as the widest bulge against the
     * parent vessel's baseline calibre.
     *
     * WHAT IT IS NOT
     * This is intensity thresholding, not clinical segmentation. It is sound on
     * bright-vessel MRA of the kind these phantoms represent; on a real study
     * with partial-volume effects, overlapping vessels or a thrombosed sac it
     * would need the Frangi/Hessian vesselness path in imaging.py, which needs
     * ITK and a server. Every measurement is returned with the assumptions it
     * rests on, and the UI lets a clinician correct it.
     */
    function measureSac(buffer, tags) {
        const px = pixels(buffer, tags);
        if (!px) {
            return { ok: false, reason: "pixel data unavailable or compressed" };
        }
        const { data, rows, cols } = px;

        // Pixel spacing is [row, column] in mm. Without it a measurement in
        // pixels cannot become millimetres, and guessing would be worse than
        // declining.
        let sy = null, sx = null;
        if (typeof tags.pixelSpacing === "string" && tags.pixelSpacing.includes("\\")) {
            const parts = tags.pixelSpacing.split("\\").map(parseFloat);
            if (parts.length >= 2 && parts.every(Number.isFinite)) { sy = parts[0]; sx = parts[1]; }
        }
        if (!sy || !sx) return { ok: false, reason: "no PixelSpacing (0028,0030) in the header" };

        // --- Otsu threshold ------------------------------------------------
        let lo = Infinity, hi = -Infinity;
        for (let i = 0; i < data.length; i++) {
            if (data[i] < lo) lo = data[i];
            if (data[i] > hi) hi = data[i];
        }
        if (hi <= lo) return { ok: false, reason: "image has no intensity range" };

        const BINS = 256;
        const hist = new Float64Array(BINS);
        const scale = (BINS - 1) / (hi - lo);
        for (let i = 0; i < data.length; i++) hist[Math.round((data[i] - lo) * scale)]++;

        const total = data.length;
        let sum = 0;
        for (let b = 0; b < BINS; b++) sum += b * hist[b];
        let sumB = 0, wB = 0, best = -1, bestBin = 0;
        for (let b = 0; b < BINS; b++) {
            wB += hist[b];
            if (wB === 0) continue;
            const wF = total - wB;
            if (wF === 0) break;
            sumB += b * hist[b];
            const mB = sumB / wB, mF = (sum - sumB) / wF;
            const between = wB * wF * (mB - mF) * (mB - mF);
            if (between > best) { best = between; bestBin = b; }
        }
        const thresh = lo + bestBin / scale;

        // --- largest connected bright region (4-connected flood fill) -------
        const mask = new Uint8Array(total);
        for (let i = 0; i < total; i++) mask[i] = data[i] > thresh ? 1 : 0;

        const label = new Int32Array(total).fill(-1);
        let bestLabel = -1, bestCount = 0, next = 0;
        const stack = new Int32Array(total);
        for (let seed = 0; seed < total; seed++) {
            if (!mask[seed] || label[seed] !== -1) continue;
            let sp = 0, count = 0;
            stack[sp++] = seed;
            label[seed] = next;
            while (sp > 0) {
                const cur = stack[--sp];
                count++;
                const r = (cur / cols) | 0, c = cur % cols;
                if (c > 0        && mask[cur - 1]    && label[cur - 1]    === -1) { label[cur - 1]    = next; stack[sp++] = cur - 1; }
                if (c < cols - 1 && mask[cur + 1]    && label[cur + 1]    === -1) { label[cur + 1]    = next; stack[sp++] = cur + 1; }
                if (r > 0        && mask[cur - cols] && label[cur - cols] === -1) { label[cur - cols] = next; stack[sp++] = cur - cols; }
                if (r < rows - 1 && mask[cur + cols] && label[cur + cols] === -1) { label[cur + cols] = next; stack[sp++] = cur + cols; }
            }
            if (count > bestCount) { bestCount = count; bestLabel = next; }
            next++;
        }
        if (bestLabel < 0 || bestCount < 20) {
            return { ok: false, reason: "no vessel-like bright region found" };
        }

        // --- width profile along the vessel --------------------------------
        // Column-wise extent of the region. The parent artery contributes a
        // roughly constant width; the sac is where that width bulges.
        const widthPx = new Float64Array(cols);
        for (let c = 0; c < cols; c++) {
            let top = -1, bot = -1;
            for (let r = 0; r < rows; r++) {
                if (label[r * cols + c] === bestLabel) { if (top < 0) top = r; bot = r; }
            }
            widthPx[c] = top < 0 ? 0 : (bot - top + 1);
        }

        const present = Array.from(widthPx).filter((w) => w > 0).sort((a, b) => a - b);
        if (present.length < 5) return { ok: false, reason: "vessel too small to profile" };

        // Baseline = median column width, which the parent artery dominates
        // because it spans the whole field of view.
        const baseline = present[Math.floor(present.length / 2)];
        const parentMm = baseline * sy;

        // The sac protrudes from the parent artery, so the TOTAL column extent
        // is parent calibre PLUS protrusion — measuring that as the dome
        // over-reports badly (9.0 mm against a true 5.38 mm on PT-2026-0103,
        // +67%). The dome has to be measured on the sac ALONE.
        //
        // Find where the parent wall runs, then treat everything beyond it as
        // sac and measure that region's own extent.
        const topRow = new Int32Array(cols).fill(-1);
        const botRow = new Int32Array(cols).fill(-1);
        for (let c = 0; c < cols; c++) {
            for (let r = 0; r < rows; r++) {
                if (label[r * cols + c] === bestLabel) { if (topRow[c] < 0) topRow[c] = r; botRow[c] = r; }
            }
        }
        // Parent wall position, taken from columns at baseline width — those
        // are pure parent, uncontaminated by the sac.
        const parentTops = [], parentBots = [];
        for (let c = 0; c < cols; c++) {
            if (widthPx[c] > 0 && Math.abs(widthPx[c] - baseline) <= 1) {
                parentTops.push(topRow[c]); parentBots.push(botRow[c]);
            }
        }
        if (parentTops.length < 3) return { ok: false, reason: "could not isolate the parent artery" };
        parentTops.sort((a, b) => a - b); parentBots.sort((a, b) => a - b);
        const pTop = parentTops[Math.floor(parentTops.length / 2)];
        const pBot = parentBots[Math.floor(parentBots.length / 2)];

        // Sac = the largest CONNECTED blob outside the parent band.
        //
        // Simply taking every pixel outside the band does not work: the parent
        // wall is never perfectly straight, so its own rippling edges fall
        // outside the median band in scattered columns across the whole image.
        // The bounding box of that set spans the full field of view — it
        // reported a 72.6 mm dome on a 36-row image. A second connected-
        // component pass keeps only the contiguous bulge.
        const outside = new Uint8Array(total);
        for (let r = 0; r < rows; r++) {
            for (let c = 0; c < cols; c++) {
                const i = r * cols + c;
                if (label[i] === bestLabel && (r < pTop || r > pBot)) outside[i] = 1;
            }
        }
        const seen = new Uint8Array(total);
        let sacMinR = Infinity, sacMaxR = -Infinity, sacMinC = Infinity, sacMaxC = -Infinity, sacPx = 0;
        for (let seed = 0; seed < total; seed++) {
            if (!outside[seed] || seen[seed]) continue;
            let sp = 0, count = 0;
            let m0 = rows, m1 = -1, n0 = cols, n1 = -1;
            stack[sp++] = seed; seen[seed] = 1;
            while (sp > 0) {
                const cur = stack[--sp];
                count++;
                const r = (cur / cols) | 0, c = cur % cols;
                if (r < m0) m0 = r; if (r > m1) m1 = r;
                if (c < n0) n0 = c; if (c > n1) n1 = c;
                if (c > 0        && outside[cur - 1]    && !seen[cur - 1])    { seen[cur - 1] = 1;    stack[sp++] = cur - 1; }
                if (c < cols - 1 && outside[cur + 1]    && !seen[cur + 1])    { seen[cur + 1] = 1;    stack[sp++] = cur + 1; }
                if (r > 0        && outside[cur - cols] && !seen[cur - cols]) { seen[cur - cols] = 1; stack[sp++] = cur - cols; }
                if (r < rows - 1 && outside[cur + cols] && !seen[cur + cols]) { seen[cur + cols] = 1; stack[sp++] = cur + cols; }
            }
            if (count > sacPx) {
                sacPx = count;
                sacMinR = m0; sacMaxR = m1; sacMinC = n0; sacMaxC = n1;
            }
        }

        let domeMm, neckMm, bulge;
        if (sacPx < 10) {
            // No protrusion beyond the parent wall — a vessel without an
            // aneurysm, or a slice that missed it.
            domeMm = 0; neckMm = 0; bulge = 0;
        } else {
            // Dome = the sac's larger principal extent. Height captures a tall
            // narrow sac; width captures a broad shallow one. Taking the max
            // measures the maximum diameter, which is what the morphology and
            // the PHASES size band are both defined on.
            const sacH = (sacMaxR - sacMinR + 1) * sy;
            const sacW = (sacMaxC - sacMinC + 1) * sx;
            domeMm = Math.max(sacH, sacW);

            // Neck = the NARROWEST width across the sac, which is the ostium.
            //
            // Taking the width at the parent wall gives the sphere's equator
            // instead, because a sac seated shallowly is near its widest there
            // — that read 11.4 mm against a true 8.22 mm. The waist is what the
            // dome-to-neck ratio is defined on, so scan the sac's rows and take
            // the minimum non-zero extent.
            // Width of the sac on each of its rows.
            const rowW = new Float64Array(rows).fill(0);
            for (let r = sacMinR; r <= sacMaxR; r++) {
                let nMin = Infinity, nMax = -Infinity;
                for (let c = sacMinC; c <= sacMaxC; c++) {
                    if (label[r * cols + c] === bestLabel) {
                        if (c < nMin) nMin = c;
                        if (c > nMax) nMax = c;
                    }
                }
                if (Number.isFinite(nMin)) rowW[r] = (nMax - nMin + 1) * sx;
            }

            // Equator = widest row. Searching the WHOLE sac for its minimum
            // finds the dome's tip, where the width tapers to nothing — that
            // returned a 0.6 mm neck on a 5.4 mm sac. The ostium lies between
            // the equator and the parent wall, so only that span is searched.
            let eqRow = sacMinR, eqW = 0;
            for (let r = sacMinR; r <= sacMaxR; r++) {
                if (rowW[r] > eqW) { eqW = rowW[r]; eqRow = r; }
            }
            const junction = sacMinR < pTop ? pTop : pBot;
            const from = Math.min(eqRow, junction), to = Math.max(eqRow, junction);

            let narrow = Infinity;
            for (let r = from; r <= to; r++) {
                if (rowW[r] > 0 && rowW[r] < narrow) narrow = rowW[r];
            }
            neckMm = Number.isFinite(narrow) ? narrow : sacW;
            // The ostium cannot exceed the dome it opens from.
            neckMm = Math.min(neckMm, domeMm);
            bulge = sacMaxR - sacMinR + 1;
        }
        return {
            ok: true,
            domeDiameterMm: +domeMm.toFixed(2),
            neckDiameterMm: +neckMm.toFixed(2),
            parentDiameterMm: +parentMm.toFixed(2),
            aspectRatio: +(domeMm / Math.max(neckMm, 0.1)).toFixed(2),
            thresholdUsed: thresh,
            regionPixels: bestCount,
            pixelSpacingMm: [sy, sx],
            // A profile with no bulge is a vessel without an aneurysm, or a
            // slice that missed it. Saying so beats reporting the parent
            // artery's width as a dome.
            bulgeDetected: bulge > 0 && domeMm > 0,
            method: "Otsu threshold + largest connected component + width profile",
            caveat: "Intensity thresholding on a bright-vessel MRA, not clinical "
                  + "segmentation. Confirm the measurement before relying on it.",
        };
    }

    /**
     * Clinical history for PHASES, read from the header.
     *
     * Everything here comes from the FILE. A field the scan does not carry is
     * returned as null, not as a default — "not known" and "No" score
     * differently, and treating the first as the second invents a low-risk
     * patient out of an absent record.
     *
     * The history and diagnosis fields are free text, so they are matched on
     * clinical wording rather than an exact code. Negation is checked FIRST:
     * "NO HYPERTENSION" contains "hypertension".
     */
    function clinicalHistory(tags) {
        if (!tags) return {};
        const hist = String(tags.additionalPatientHistory || "").toUpperCase();
        const diag = String(tags.admittingDiagnoses || "").toUpperCase();
        const both = `${hist} ${diag}`;

        // "045Y" | "045M" | "045D" — only years are meaningful for PHASES.
        let age = null;
        const mAge = /^(\d{1,3})\s*Y?$/.exec(String(tags.patientAge || "").trim());
        if (mAge) age = parseInt(mAge[1], 10);

        const yesNo = (positive, negative) => {
            if (negative.test(both)) return false;
            if (positive.test(both)) return true;
            return null;
        };

        const pop = String(tags.ethnicGroup || "").trim().toLowerCase();
        const population = pop.startsWith("japan") ? "Japanese"
                         : pop.startsWith("finn") ? "Finnish"
                         : pop ? "Other" : null;

        // PHASES groups ACOM, PCOM and the posterior circulation together.
        let site = null;
        if (/ANTERIOR COMMUNICATING|ACOM|POSTERIOR COMMUNICATING|PCOM|BASILAR|VERTEBRAL|POSTERIOR CIRCULATION/.test(both)) {
            site = "ACOM_PCOM_POST";
        } else if (/MIDDLE CEREBRAL|MCA/.test(both)) {
            site = "MCA";
        } else if (/INTERNAL CAROTID|ICA/.test(both)) {
            site = "ICA";
        }

        return {
            age,
            hypertension: yesNo(/HYPERTENS/, /NO HYPERTENS|WITHOUT HYPERTENS|DENIES HYPERTENS/),
            earlierSAH: yesNo(/PRIOR SAH|PREVIOUS SAH|SUBARACHNOID/,
                              /NO PRIOR SAH|NO PREVIOUS SAH|NO SUBARACHNOID/),
            population,
            site,
            sex: tags.patientSex || null,
        };
    }

    global.NeuroDicom = { parse, pixels, measureSac, clinicalHistory, TAGS };
})(typeof window !== "undefined" ? window : globalThis);
