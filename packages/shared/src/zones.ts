/**
 * Hemodynamic zones.
 *
 * The legacy app had a latent bug worth understanding before touching this:
 * drawHeatmap() indexed `patient.zones[0..3]` POSITIONALLY (app.js:469-472)
 * while computeRiskBreakdown(), updateRadialGauges(), openReportModal() and
 * neuro3d.js all looked the same zones up BY NAME ("Aneurysm Dome"). Both
 * worked only because the hardcoded array happened to be in a fixed order.
 *
 * Real CFD output has no such guarantee — patch iteration order is whatever
 * OpenFOAM emits — so the heatmap would have silently mis-coloured. The ZoneId
 * union removes both failure modes, and it maps 1:1 onto the OpenFOAM patch
 * names produced by the four-region STL (inlet/outlet/wall/wall_aneurysm).
 */

export type ZoneId = 'inlet' | 'outlet' | 'neck' | 'dome';

export const ZONE_IDS: readonly ZoneId[] = ['inlet', 'outlet', 'neck', 'dome'];

export interface Zone {
  id: ZoneId;
  /** Human-readable label for the UI, e.g. "Aneurysm Dome". */
  label: string;
  /** OpenFOAM patch this zone was sampled from. */
  patch?: string;
  /** Time-Averaged Wall Shear Stress, Pa (already converted from kinematic). */
  tawss: number;
  /** Oscillatory Shear Index, 0..0.5. */
  osi: number;
  /** Relative Residence Time, Pa^-1. */
  rrt?: number;
  /** Endothelial Cell Activation Potential, Pa^-1. */
  ecap?: number;
  /** Area-weighted patch area, mm^2. */
  areaMm2?: number;
  isAneurysm: boolean;
}

/** Maps a zone to the OpenFOAM patch it is sampled from. */
export const ZONE_TO_PATCH: Record<ZoneId, string> = {
  inlet: 'inlet',
  outlet: 'outlet',
  neck: 'wall_aneurysm',
  dome: 'wall_aneurysm',
};

/**
 * Index zones by id.
 *
 * Throws on a missing zone rather than returning undefined: a silently absent
 * dome would propagate as NaN through the entire risk calculation and surface
 * as a blank gauge instead of an error.
 */
export function byZone(zones: readonly Zone[]): Record<ZoneId, Zone> {
  const out = {} as Record<ZoneId, Zone>;
  for (const z of zones) out[z.id] = z;
  const missing = ZONE_IDS.filter((id) => !(id in out));
  if (missing.length > 0) {
    throw new Error(`missing hemodynamic zone(s): ${missing.join(', ')}`);
  }
  return out;
}

/** The dome drives every risk metric; this is the canonical accessor. */
export function domeOf(zones: readonly Zone[]): Zone {
  return byZone(zones).dome;
}
