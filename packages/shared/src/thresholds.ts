/**
 * Clinical thresholds and normalisation ranges — single source of truth.
 *
 * These were previously scattered across app.js and neuro3d.js (and one stale
 * comment disagreed with the code). Centralising them is what stops the 2D
 * heatmap, the 3D viewer, the gauges and the PDF report from drifting apart.
 */

/**
 * Alert thresholds — "is this value clinically concerning?"
 *
 * OSI_HIGH is 0.2, NOT the 0.3 the legacy app.js used (lines 633, 808, 853 and
 * index.html:246). The SAD and the literature both use >0.2; the 0.3 appears to
 * have been arbitrary. This is a deliberate reconciliation, and it CHANGES THE
 * DEMO: PT-2025-0037's dome sits at OSI 0.24, which was previously silent and
 * now raises an alert. Golden tests pin that behaviour so it can't drift back.
 */
export const THRESHOLDS = {
  /** Low wall shear stress, Pa. Below this, endothelial dysfunction is implicated. */
  TAWSS_LOW_PA: 0.4,
  /** Oscillatory Shear Index, dimensionless 0..0.5. */
  OSI_HIGH: 0.2,
  /** Relative Residence Time, Pa^-1. */
  RRT_HIGH: 3.0,
  /** Endothelial Cell Activation Potential, Pa^-1. */
  ECAP_HIGH: 1.0,
  /** Aneurysm dome max diameter, mm. */
  DIAMETER_HIGH_MM: 5.0,
  /** Dome height / neck width. */
  ASPECT_RATIO_HIGH: 1.5,
} as const;

/**
 * Normalisation ranges for the Composite Risk Index.
 *
 * IMPORTANT: these are a *separately calibrated scale* and are NOT the same as
 * the alert thresholds above. In particular OSI normalises over 0.03..0.35 —
 * changing that to match OSI_HIGH would shift every patient's composite score
 * and invalidate the golden tests. Leave them alone unless you intend to
 * recalibrate the index itself.
 */
export const RISK_RANGES = {
  /** TAWSS is inverted: LOW shear is the risk factor. */
  tawss: { min: 0.15, max: 1.5 },
  osi: { min: 0.03, max: 0.35 },
  diameterMm: { min: 2.0, max: 10.0 },
  aspectRatio: { min: 0.7, max: 2.5 },
} as const;

/** Composite Risk Index component weights. Must sum to 1.0. */
export const RISK_WEIGHTS = {
  tawss: 0.35,
  osi: 0.3,
  diameter: 0.2,
  aspect: 0.15,
} as const;

/** Composite score -> tier cut-offs (inclusive lower bounds). */
export const TIER_CUTOFFS = { high: 75, moderate: 45 } as const;

/** Guard against the OSI -> 0.5 singularity in RRT, and TAWSS -> 0 in ECAP. */
export const DIVISION_FLOOR = 0.02;

/** Blood properties. Also the kinematic->Pa conversion factor for OpenFOAM WSS. */
export const BLOOD = {
  DENSITY_KG_M3: 1060,
  VISCOSITY_PA_S: 0.0035,
  /** nu = mu / rho, m^2/s — what OpenFOAM's physicalProperties actually wants. */
  KINEMATIC_VISCOSITY_M2_S: 0.0035 / 1060,
} as const;
