/**
 * Clinical and calibration thresholds — one definition, read by everything.
 *
 * WHY THIS FILE EXISTS
 * These numbers were previously written out as literals wherever they were
 * needed: the risk index in app.js, the heatmap colour ramp in app.js, the 3D
 * sac colouring in neuro3d.js, and the worker's hemodynamics.py. They drifted,
 * exactly as duplicated constants do. The OSI alert was 0.3 in the dashboard
 * and 0.2 in the worker, so the same solve was "elevated" on the server and
 * unremarkable in the browser, and nothing anywhere pointed that out.
 *
 * THE OSI BAND, AND WHY IT CHANGED
 * OSI_RISK_LOW/HIGH used to be 0.03 and 0.35. Those came from the three curated
 * demonstration cases, whose dome OSI values were authored as 0.08, 0.24 and
 * 0.38 — not solved, not measured, and not physical for an AREA-AVERAGED sac
 * OSI. 0.5 is the theoretical maximum, requiring the wall shear vector to
 * reverse so completely that its time-mean is zero across the whole sac.
 *
 * Every real transient solve lands two orders of magnitude below the old floor:
 *
 *     case            dome OSI    parent artery OSI
 *     PT-2026-0103     0.0130          0.00023
 *     PT-2026-0101     0.0124          0.00031
 *     synthetic01      0.0096          0.00022
 *
 * So the band's lower bound sat above every value the physics can produce. The
 * clamp pinned the OSI risk term to exactly zero for every real case, forever,
 * and a term carrying 30% of the composite weight contributed nothing. That is
 * why the index looked like it ignored the uploaded file: for a third of its
 * weight, it did.
 *
 * The replacement is anchored at both ends on solved data, and will tighten as
 * more transient solves land.
 */
(function (global) {
    "use strict";

    const T = {
        /**
         * Time-averaged wall shear stress, Pa.
         *
         * TAWSS_LOW is the clinical low-shear threshold below which endothelial
         * dysfunction and wall degeneration are associated.
         *
         * THE RISK BAND USED TO RUN 1.5 -> 0.15 Pa AND THAT WAS THE SECOND
         * UNREACHABLE-BAND BUG IN THIS FILE.
         *
         * 1.5 Pa is healthy PARENT-ARTERY shear. But this term scores the SAC,
         * and a sac has low shear by definition — slowing and recirculating the
         * flow is what an aneurysm does. Every geometry in the calibrated range
         * produces a sac TAWSS between 0.18 and 0.41 Pa, which against a
         * 0.15-1.5 band scores 81% to 98%. A term carrying 35% of the composite
         * weight was contributing a near-constant ~31 points to every case: it
         * added an offset and discriminated nothing.
         *
         * That is the mirror image of the OSI bug below — that band's FLOOR was
         * above every real value so the term pinned at 0; this band's CEILING
         * was above every real value so the term pinned near 100. Both make a
         * weighted term into a constant, and neither is visible without pushing
         * two different cases through and comparing.
         *
         * The band now runs from the clinical low-shear threshold (0.4 Pa —
         * at or above it there is no low-shear risk to score) down to 0.10 Pa,
         * below the lowest value any solve here has produced (0.14 Pa, on
         * PT-2026-0102). Sac TAWSS now spans 0% to 74% of the term instead of
         * 81% to 98%.
         */
        TAWSS_LOW_PA: 0.4,
        TAWSS_RISK_HIGH_PA: 0.40,
        TAWSS_RISK_LOW_PA: 0.10,

        /**
         * Area-averaged sac OSI, dimensionless, 0..0.5.
         *
         * LOW  0.002  An order of magnitude above the parent artery's own OSI
         *             (~0.0002). A sac this unidirectional flows like a healthy
         *             vessel and carries no oscillatory risk.
         * HIGH 0.030  About 2.3x the highest sac OSI solved so far. Reaching it
         *             means oscillation well outside anything this geometry
         *             family has produced, which is what the top of a risk scale
         *             should mean.
         */
        OSI_RISK_LOW: 0.002,
        OSI_RISK_HIGH: 0.030,

        /**
         * "Elevated" for an area-averaged sac OSI.
         *
         * The dashboard alerted above 0.3 and the worker above 0.2. Both are
         * literature figures for POINT-WISE or peak OSI, and both were applied
         * to an area-weighted mean over the entire sac. Those are different
         * statistics: a sac can carry local OSI above 0.4 in its recirculation
         * core while averaging 0.012 overall. Against a peak-derived threshold
         * the mean cannot trip at all, so the alert was not conservative — it
         * was dead.
         *
         * Set at the top of the calibrated band. It means "elevated relative to
         * the range this solver has produced", which is what there is evidence
         * for, and not a clinical finding.
         */
        OSI_ELEVATED: 0.030,

        RRT_HIGH: 3.0,
        ECAP_HIGH: 1.0,

        /**
         * Composite Risk Index tier boundaries.
         *
         * These were 45 and 75, and with the old TAWSS band the index could only
         * reach 42.5 to 75.8 across the ENTIRE geometry space — 2 to 30 mm dome,
         * aspect ratio 0.5 to 3.5. Almost the whole reachable range fell inside
         * the Moderate band, so every case on the site read Moderate regardless
         * of its geometry. Low required a 2 mm "aneurysm" at aspect ratio 0.5,
         * which is not an aneurysm, and High required the extreme corner.
         *
         * Fixing the TAWSS band moves the reachable range to 14.2 - 67.3, which
         * fixes Low but leaves High mathematically unreachable — the same defect
         * in the other direction.
         *
         * So the boundaries are placed on the range the index can ACTUALLY
         * produce, at roughly one third and three fifths of it:
         *
         *     reachable   14.2 ................................ 67.3
         *     Low         <32
         *     Moderate         32 ......... 55
         *     High                            >=55
         *
         * They are constants rather than computed at runtime, so a case cannot
         * change tier because some other case was added. A test asserts all
         * three tiers stay reachable; if refitting the surrogate moves the
         * range, that test fails and these get revisited deliberately rather
         * than drifting.
         */
        CRI_MODERATE: 32,
        CRI_HIGH: 55,
    };

    /** Normalise a value into 0..1 over [lo, hi], clamped. */
    T.band = function (value, lo, hi) {
        if (!(hi > lo)) return 0;
        return Math.max(0, Math.min(1, (value - lo) / (hi - lo)));
    };

    global.NeuroThresholds = T;
})(typeof window !== "undefined" ? window : globalThis);
