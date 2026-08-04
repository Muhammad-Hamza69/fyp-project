/**
 * Rupture-risk scoring.
 *
 * Ported VERBATIM from the legacy app.js:99-209. This is the one genuinely
 * defensible piece of the original project — everything upstream of it was
 * scripted animation — so the arithmetic is preserved exactly and pinned by
 * golden tests against the three original demo patients.
 *
 * Two independent models are computed and shown side by side, deliberately:
 *
 *   1. Composite Risk Index — a weighted blend of hemodynamics (TAWSS, OSI) and
 *      morphology (diameter, aspect ratio). Project-specific, transparent.
 *   2. PHASES (Greving et al., Lancet Neurol 2014) — the established clinical
 *      instrument, based on population/comorbidity/anatomy. Independent of CFD.
 *
 * They answer different questions and can legitimately disagree; showing both
 * is more honest than blending them into one number.
 */

import {
  DIVISION_FLOOR,
  RISK_RANGES,
  RISK_WEIGHTS,
  TIER_CUTOFFS,
} from './thresholds';
import { domeOf, type Zone } from './zones';

export type RiskTier = 'Low' | 'Moderate' | 'High';
export type Population = 'Other' | 'Japanese' | 'Finnish';
export type AneurysmSite = 'ICA' | 'MCA' | 'ACOM_PCOM_POST';

export interface Morphology {
  maxDiameterMm: number;
  aspectRatio: number;
  volumeMm3?: number;
  domeToNeck?: number;
  ostiumAreaMm2?: number;
  undulationIndex?: number;
  nonSphericityIndex?: number;
  tortuosity?: number;
}

export interface Demographics {
  age: number;
  sex?: 'M' | 'F' | 'O';
  hypertension: boolean;
  earlierSAH: boolean;
  population: Population;
  site: AneurysmSite;
}

export interface RiskInput {
  zones: readonly Zone[];
  morphology: Morphology;
  demographics: Demographics;
}

export interface RiskBreakdown {
  tawssScore: number;
  osiScore: number;
  diameterScore: number;
  aspectScore: number;
  /** Rounded 0..100. */
  composite: number;
}

export interface PhasesItem {
  label: string;
  value: string;
  points: number;
}

export interface PhasesResult {
  items: PhasesItem[];
  points: number;
  /** Cumulative 5-year rupture risk, %. */
  riskPercent: number;
}

export interface TierInfo {
  riskLevel: RiskTier;
  riskLabel: string;
  /** Stable token for styling — replaces the legacy `.color-*-risk` class
   *  names, which app.js emitted but which never existed in style.css. */
  tierToken: 'low' | 'moderate' | 'high';
}

// ---------------------------------------------------------------------------
// Composite Risk Index
// ---------------------------------------------------------------------------

export function clamp01(v: number): number {
  return Math.max(0, Math.min(1, v));
}

function normalise(value: number, min: number, max: number): number {
  return clamp01((value - min) / (max - min)) * 100;
}

/**
 * Per-factor 0..100 sub-scores plus the weighted composite.
 *
 * Note TAWSS is INVERTED — low wall shear stress is the risk factor (Meng et
 * al.'s low-WSS hypothesis), so the score rises as TAWSS falls.
 */
export function computeRiskBreakdown(input: RiskInput): RiskBreakdown {
  const dome = domeOf(input.zones);
  const { maxDiameterMm, aspectRatio } = input.morphology;

  const tawssScore =
    clamp01(
      (RISK_RANGES.tawss.max - dome.tawss) /
        (RISK_RANGES.tawss.max - RISK_RANGES.tawss.min),
    ) * 100;
  const osiScore = normalise(dome.osi, RISK_RANGES.osi.min, RISK_RANGES.osi.max);
  const diameterScore = normalise(
    maxDiameterMm,
    RISK_RANGES.diameterMm.min,
    RISK_RANGES.diameterMm.max,
  );
  const aspectScore = normalise(
    aspectRatio,
    RISK_RANGES.aspectRatio.min,
    RISK_RANGES.aspectRatio.max,
  );

  const composite =
    tawssScore * RISK_WEIGHTS.tawss +
    osiScore * RISK_WEIGHTS.osi +
    diameterScore * RISK_WEIGHTS.diameter +
    aspectScore * RISK_WEIGHTS.aspect;

  return {
    tawssScore,
    osiScore,
    diameterScore,
    aspectScore,
    composite: Math.round(composite),
  };
}

export function computeCompositeRisk(input: RiskInput): number {
  return computeRiskBreakdown(input).composite;
}

export function getRiskTier(score: number): TierInfo {
  if (score >= TIER_CUTOFFS.high) {
    return { riskLevel: 'High', riskLabel: 'High Rupture Risk', tierToken: 'high' };
  }
  if (score >= TIER_CUTOFFS.moderate) {
    return {
      riskLevel: 'Moderate',
      riskLabel: 'Moderate Risk Profile',
      tierToken: 'moderate',
    };
  }
  return { riskLevel: 'Low', riskLabel: 'Stable / Low Risk', tierToken: 'low' };
}

// ---------------------------------------------------------------------------
// Supplementary hemodynamic markers
// ---------------------------------------------------------------------------

/**
 * Relative Residence Time, Pa^-1.  RRT ~ 1 / ((1 - 2*OSI) * TAWSS)
 *
 * The denominator vanishes as OSI -> 0.5 (fully oscillatory flow), so it is
 * floored. Without that guard a fully-reversing region returns Infinity and
 * every downstream gauge renders blank.
 */
export function computeRRT(dome: Pick<Zone, 'tawss' | 'osi'>): number {
  const denom = Math.max(DIVISION_FLOOR, (1 - 2 * dome.osi) * dome.tawss);
  return 1 / denom;
}

/**
 * Endothelial Cell Activation Potential, Pa^-1.  ECAP = OSI / TAWSS
 *
 * Above ~1.0 the oscillatory component dominates mean shear — a combination
 * associated with endothelial dysfunction and wall degradation.
 */
export function computeECAP(dome: Pick<Zone, 'tawss' | 'osi'>): number {
  return dome.osi / Math.max(DIVISION_FLOOR, dome.tawss);
}

// ---------------------------------------------------------------------------
// PHASES score (Greving et al. 2014)
// ---------------------------------------------------------------------------

export const PHASES_SITE_LABELS: Record<AneurysmSite, string> = {
  ICA: 'Internal Carotid Artery (ICA)',
  MCA: 'Middle Cerebral Artery (MCA)',
  ACOM_PCOM_POST: 'Ant./Post. Communicating or Posterior Circulation',
};

const PHASES_SITE_POINTS: Record<AneurysmSite, number> = {
  ICA: 0,
  MCA: 2,
  ACOM_PCOM_POST: 4,
};

const PHASES_POPULATION_POINTS: Record<Population, number> = {
  Other: 0, // North American / European
  Japanese: 3,
  Finnish: 5,
};

/** Cumulative 5-year rupture risk (%) by total PHASES points. */
const PHASES_RISK_TABLE: readonly { max: number; percent: number }[] = [
  { max: 1, percent: 0.4 },
  { max: 3, percent: 0.7 },
  { max: 4, percent: 0.9 },
  { max: 5, percent: 1.3 },
  { max: 6, percent: 1.7 },
  { max: 7, percent: 2.4 },
  { max: 8, percent: 3.2 },
  { max: 9, percent: 4.3 },
  { max: 10, percent: 5.3 },
  { max: 11, percent: 7.2 },
  { max: Infinity, percent: 17.8 },
];

export function phasesRiskPercentFromPoints(points: number): number {
  const bracket = PHASES_RISK_TABLE.find((b) => points <= b.max);
  // The table's final entry is Infinity, so this is unreachable in practice.
  return bracket ? bracket.percent : PHASES_RISK_TABLE[PHASES_RISK_TABLE.length - 1].percent;
}

export function computePhasesScore(input: RiskInput): PhasesResult {
  const d = input.demographics;
  const diameter = input.morphology.maxDiameterMm;

  let sizePoints = 0;
  if (diameter >= 20.0) sizePoints = 10;
  else if (diameter >= 10.0) sizePoints = 6;
  else if (diameter >= 7.0) sizePoints = 3;

  const items: PhasesItem[] = [
    {
      label: 'Population',
      value: d.population,
      points: PHASES_POPULATION_POINTS[d.population] ?? 0,
    },
    { label: 'Hypertension', value: d.hypertension ? 'Yes' : 'No', points: d.hypertension ? 1 : 0 },
    { label: 'Age', value: `${d.age} yrs`, points: d.age >= 70 ? 1 : 0 },
    { label: 'Size of Aneurysm', value: `${diameter.toFixed(1)} mm`, points: sizePoints },
    {
      label: 'Earlier SAH (other aneurysm)',
      value: d.earlierSAH ? 'Yes' : 'No',
      points: d.earlierSAH ? 1 : 0,
    },
    {
      label: 'Site of Aneurysm',
      value: PHASES_SITE_LABELS[d.site],
      points: PHASES_SITE_POINTS[d.site] ?? 0,
    },
  ];

  const points = items.reduce((sum, i) => sum + i.points, 0);
  return { items, points, riskPercent: phasesRiskPercentFromPoints(points) };
}

// ---------------------------------------------------------------------------
// Shared colour normalisation (2D heatmap + 3D viewer must agree)
// ---------------------------------------------------------------------------

export type MapMode = 'TAWSS' | 'OSI';

/**
 * 0..1 risk factor for colour interpolation (blue -> red).
 *
 * The legacy code implemented this TWICE — getInterpolatedColor() in app.js and
 * riskFactorForZone() in neuro3d.js — with a stale comment in one of them. Any
 * edit to one silently desynchronised the 2D heatmap from the 3D model. One
 * implementation now, imported by both.
 */
export function riskFactor(
  zone: Pick<Zone, 'tawss' | 'osi' | 'isAneurysm'>,
  mode: MapMode,
): number {
  let factor: number;
  if (mode === 'OSI') {
    factor = clamp01(
      (zone.osi - RISK_RANGES.osi.min) / (RISK_RANGES.osi.max - RISK_RANGES.osi.min),
    );
  } else {
    factor =
      1.0 -
      clamp01(
        (zone.tawss - RISK_RANGES.tawss.min) /
          (RISK_RANGES.tawss.max - RISK_RANGES.tawss.min),
      );
  }
  // Non-aneurysm zones are damped so the sac visually dominates.
  if (!zone.isAneurysm) factor *= 0.2;
  return factor;
}
