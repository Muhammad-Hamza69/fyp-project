/**
 * Hemodynamic surrogate — browser-side evaluation, microseconds.
 *
 * WHY THIS EXISTS
 * A Navier-Stokes solve takes hours. One cardiac cycle at 239k cells ran ~10 h
 * on the machine that produced this project's results, and the cost is
 * irreducible: the Courant condition fixes the timestep, which fixes the step
 * count, and each step needs a global pressure solve. No web upload can wait
 * for that, and any tool claiming otherwise is not solving anything.
 *
 * The way out is not to solve faster but to solve ONCE PER GEOMETRY, not once
 * per user. services/worker/run_sweep.py solves the geometry family properly
 * with OpenFOAM; services/worker/pipeline/surrogate.py fits a response surface
 * through those solutions and exports its coefficients to models/surrogate.json.
 * This file evaluates that surface — a handful of logarithms and multiplies.
 *
 * WHAT IS AND IS NOT PREDICTED
 * The parent artery is not fitted at all. Wall shear in fully developed pipe
 * flow is known in closed form, tau = 4*mu*Q/(pi*R^3), so it is computed
 * exactly and one measured correction factor is applied.
 *
 * OSI and ECAP are NOT predicted. They require a cardiac cycle; the calibration
 * set is steady, so there is nothing in it from which to estimate oscillation.
 * Returning a plausible-looking OSI here would undo the whole point of showing
 * "n/a" everywhere else in this dashboard.
 *
 * Predictions carry their leave-one-out cross-validation error and a flag when
 * the query falls outside the calibrated range, because a response surface
 * fitted over 3-12 mm says nothing dependable about a 25 mm giant aneurysm.
 */

(function (global) {
    "use strict";

    let MODEL = null;

    async function load(url) {
        if (MODEL) return MODEL;
        const res = await fetch(url || "models/surrogate.json");
        if (!res.ok) throw new Error(`surrogate model unavailable (HTTP ${res.status})`);
        MODEL = await res.json();
        return MODEL;
    }

    function isLoaded() { return MODEL !== null; }

    /** Closed-form wall shear for fully developed laminar pipe flow, in Pa. */
    function poiseuilleWss(qM3s, rM) {
        return (4 * MODEL.mu_pa_s * qM3s) / (Math.PI * Math.pow(rM, 3));
    }

    /**
     * Estimate hemodynamics from geometry.
     *
     * @param {object} g  { maxDiameterMm, neckDiameterMm?, aspectRatio? }
     * @returns {object}  hemodynamics + the evidence for trusting them
     */
    function predict(g) {
        if (!MODEL) throw new Error("surrogate model not loaded");

        const d = Number(g.maxDiameterMm);
        if (!Number.isFinite(d) || d <= 0) {
            throw new Error("a positive dome diameter is required");
        }
        const neck = Number.isFinite(Number(g.neckDiameterMm)) && Number(g.neckDiameterMm) > 0
            ? Number(g.neckDiameterMm) : d * 0.75;
        const neckRatio = neck / d;

        const parent = poiseuilleWss(MODEL.q_m3s, MODEL.r_parent_m) * MODEL.parent_correction;

        const [a, b, c] = MODEL.nwss_coef;
        const nwss = Math.min(1, Math.max(1e-4,
            Math.exp(a + b * Math.log(d) + c * Math.log(Math.max(neckRatio, 1e-6)))));
        const sac = parent * nwss;

        const [ra, rb] = MODEL.rrt_coef;
        const rrt = Math.exp(ra + rb * Math.log(Math.max(sac, 1e-6)));

        const [la, lb] = MODEL.lsar_coef;
        const lsar = Math.min(1, Math.max(0, la + lb * Math.log(d)));

        // Extrapolation is surfaced, never hidden.
        const [lo, hi] = MODEL.diameter_range_mm;
        const [nlo, nhi] = MODEL.neck_ratio_range;
        const warnings = [];
        if (d < lo || d > hi) {
            warnings.push(`Dome diameter ${d.toFixed(1)} mm is outside the calibrated `
                        + `${lo.toFixed(1)}–${hi.toFixed(1)} mm range; this is an extrapolation.`);
        }
        if (neckRatio < nlo || neckRatio > nhi) {
            warnings.push(`Neck/dome ratio ${neckRatio.toFixed(2)} is outside the calibrated `
                        + `${nlo.toFixed(2)}–${nhi.toFixed(2)} range.`);
        }

        return {
            method: "surrogate",
            parentTawss: parent,
            sacTawss: sac,
            nwss,
            rrt,
            lsarRelative: lsar,
            // Deliberately absent. See the header.
            osi: null,
            ecap: null,
            calibrationPoints: MODEL.n_points,
            looErrorPct: MODEL.loo_error_pct || {},
            extrapolating: warnings.length > 0,
            warnings,
        };
    }

    /**
     * Zones in the shape the dashboard already renders, so a surrogate result
     * flows through the existing gauges, heatmap and 3D view unchanged.
     *
     * OSI is left at 0 in the zone objects because the schema requires a
     * number, and `hemodynamics.transient = false` is what actually drives the
     * gauges to display "n/a". The zero is never shown.
     */
    function toZones(p) {
        return [
            { name: "Parent Artery Inlet", id: "3891", x: 160, y: 278, radius: 55,
              tawss: p.parentTawss, osi: 0, isAneurysm: false },
            { name: "Parent Artery Outlet", id: "3942", x: 470, y: 278, radius: 55,
              tawss: p.parentTawss * 0.94, osi: 0, isAneurysm: false },
            { name: "Aneurysm Neck", id: "4109", x: 320, y: 220, radius: 35,
              tawss: p.sacTawss * 1.45, osi: 0, isAneurysm: true },
            { name: "Aneurysm Dome", id: "4289", x: 320, y: 120, radius: 50,
              tawss: p.sacTawss, osi: 0, isAneurysm: true },
        ];
    }

    global.NeuroSurrogate = { load, isLoaded, predict, toZones };
})(typeof window !== "undefined" ? window : globalThis);
