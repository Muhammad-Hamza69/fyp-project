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

    global.NeuroDicom = { parse, TAGS };
})(typeof window !== "undefined" ? window : globalThis);
