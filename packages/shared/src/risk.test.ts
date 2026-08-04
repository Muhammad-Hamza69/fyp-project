/**
 * Golden tests pinning the ported risk math to the ORIGINAL app.js behaviour.
 *
 * These exist so the migration is provably lossless. Every expected value below
 * was derived by hand from app.js:99-209 against the three demo patients
 * hardcoded at app.js:30-93. If a refactor changes any of these numbers, the
 * refactor is wrong — not the test.
 *
 * The one intentional behaviour change (OSI alert threshold 0.3 -> 0.2) is
 * pinned explicitly at the bottom so it can't silently revert.
 */

import { describe, expect, it } from 'vitest';

import {
  computeECAP,
  computeCompositeRisk,
  computePhasesScore,
  computeRRT,
  computeRiskBreakdown,
  getRiskTier,
  riskFactor,
  type RiskInput,
} from './risk';
import { THRESHOLDS } from './thresholds';
import { byZone, type Zone } from './zones';

function zones(neck: [number, number], dome: [number, number]): Zone[] {
  return [
    { id: 'inlet', label: 'Parent Artery Inlet', tawss: 1.85, osi: 0.03, isAneurysm: false },
    { id: 'outlet', label: 'Parent Artery Outlet', tawss: 1.62, osi: 0.04, isAneurysm: false },
    { id: 'neck', label: 'Aneurysm Neck', tawss: neck[0], osi: neck[1], isAneurysm: true },
    { id: 'dome', label: 'Aneurysm Dome', tawss: dome[0], osi: dome[1], isAneurysm: true },
  ];
}

/** PT-2025-0041 — the High-risk demo case. */
const PT_0041: RiskInput = {
  zones: zones([0.35, 0.32], [0.18, 0.38]),
  morphology: { maxDiameterMm: 8.4, aspectRatio: 2.1 },
  demographics: {
    age: 72,
    hypertension: true,
    earlierSAH: false,
    population: 'Other',
    site: 'MCA',
  },
};

/** PT-2025-0037 — Moderate. */
const PT_0037: RiskInput = {
  zones: zones([0.48, 0.22], [0.42, 0.24]),
  morphology: { maxDiameterMm: 5.2, aspectRatio: 1.4 },
  demographics: {
    age: 58,
    hypertension: false,
    earlierSAH: false,
    population: 'Other',
    site: 'ICA',
  },
};

/** PT-2025-0039 — Low. */
const PT_0039: RiskInput = {
  zones: zones([0.72, 0.12], [0.85, 0.08]),
  morphology: { maxDiameterMm: 3.1, aspectRatio: 0.9 },
  demographics: {
    age: 45,
    hypertension: false,
    earlierSAH: false,
    population: 'Other',
    site: 'ICA',
  },
};

describe('Composite Risk Index — golden values from legacy app.js', () => {
  it('PT-2025-0041 scores 92 / High', () => {
    const b = computeRiskBreakdown(PT_0041);
    expect(b.tawssScore).toBeCloseTo(97.7778, 3);
    // OSI 0.38 exceeds the 0.35 range ceiling, so it clamps to exactly 100.
    expect(b.osiScore).toBe(100);
    expect(b.diameterScore).toBeCloseTo(80, 6);
    expect(b.aspectScore).toBeCloseTo(77.7778, 3);
    expect(b.composite).toBe(92);
    expect(getRiskTier(b.composite).riskLevel).toBe('High');
  });

  it('PT-2025-0037 scores 62 / Moderate', () => {
    const b = computeRiskBreakdown(PT_0037);
    expect(b.tawssScore).toBeCloseTo(80, 6);
    expect(b.osiScore).toBeCloseTo(65.625, 3);
    expect(b.diameterScore).toBeCloseTo(40, 6);
    expect(b.aspectScore).toBeCloseTo(38.8889, 3);
    expect(b.composite).toBe(62);
    expect(getRiskTier(b.composite).riskLevel).toBe('Moderate');
  });

  it('PT-2025-0039 scores 26 / Low', () => {
    const b = computeRiskBreakdown(PT_0039);
    expect(b.tawssScore).toBeCloseTo(48.1481, 3);
    expect(b.osiScore).toBeCloseTo(15.625, 3);
    expect(b.diameterScore).toBeCloseTo(13.75, 6);
    expect(b.aspectScore).toBeCloseTo(11.1111, 3);
    expect(b.composite).toBe(26);
    expect(getRiskTier(b.composite).riskLevel).toBe('Low');
  });

  it('orders the three cases High > Moderate > Low', () => {
    expect(computeCompositeRisk(PT_0041)).toBeGreaterThan(computeCompositeRisk(PT_0037));
    expect(computeCompositeRisk(PT_0037)).toBeGreaterThan(computeCompositeRisk(PT_0039));
  });
});

describe('tier boundaries', () => {
  it('is inclusive at the cut-offs', () => {
    expect(getRiskTier(75).riskLevel).toBe('High');
    expect(getRiskTier(74).riskLevel).toBe('Moderate');
    expect(getRiskTier(45).riskLevel).toBe('Moderate');
    expect(getRiskTier(44).riskLevel).toBe('Low');
    expect(getRiskTier(0).riskLevel).toBe('Low');
    expect(getRiskTier(100).riskLevel).toBe('High');
  });
});

describe('RRT and ECAP', () => {
  it('matches hand-computed values for PT-2025-0041', () => {
    const dome = byZone(PT_0041.zones).dome;
    // denom = (1 - 2*0.38) * 0.18 = 0.24 * 0.18 = 0.0432 -> 1/0.0432
    expect(computeRRT(dome)).toBeCloseTo(23.1481, 3);
    expect(computeECAP(dome)).toBeCloseTo(2.1111, 3);
  });

  it('guards the OSI -> 0.5 singularity instead of returning Infinity', () => {
    const pathological = { tawss: 0.5, osi: 0.5 }; // (1 - 2*0.5) = 0
    const rrt = computeRRT(pathological);
    expect(Number.isFinite(rrt)).toBe(true);
    expect(rrt).toBe(1 / 0.02);
  });

  it('guards TAWSS -> 0 in ECAP', () => {
    expect(Number.isFinite(computeECAP({ tawss: 0, osi: 0.3 }))).toBe(true);
  });
});

describe('PHASES score (Greving et al. 2014)', () => {
  it('PT-2025-0041: Other 0 + HTN 1 + age>=70 1 + size 8.4mm 3 + SAH 0 + MCA 2 = 7', () => {
    const p = computePhasesScore(PT_0041);
    expect(p.points).toBe(7);
    expect(p.riskPercent).toBe(2.4);
    expect(p.items).toHaveLength(6);
  });

  it('PT-2025-0037 and PT-2025-0039 both score 0 -> 0.4%', () => {
    expect(computePhasesScore(PT_0037).points).toBe(0);
    expect(computePhasesScore(PT_0037).riskPercent).toBe(0.4);
    expect(computePhasesScore(PT_0039).points).toBe(0);
    expect(computePhasesScore(PT_0039).riskPercent).toBe(0.4);
  });

  it('applies the size bands at their exact boundaries', () => {
    const at = (mm: number) =>
      computePhasesScore({ ...PT_0039, morphology: { maxDiameterMm: mm, aspectRatio: 1 } })
        .points;
    expect(at(6.9)).toBe(0);
    expect(at(7.0)).toBe(3);
    expect(at(9.9)).toBe(3);
    expect(at(10.0)).toBe(6);
    expect(at(19.9)).toBe(6);
    expect(at(20.0)).toBe(10);
  });

  it('scores population and site correctly', () => {
    const withDemo = (over: Partial<RiskInput['demographics']>) =>
      computePhasesScore({ ...PT_0039, demographics: { ...PT_0039.demographics, ...over } })
        .points;
    expect(withDemo({ population: 'Japanese' })).toBe(3);
    expect(withDemo({ population: 'Finnish' })).toBe(5);
    expect(withDemo({ site: 'MCA' })).toBe(2);
    expect(withDemo({ site: 'ACOM_PCOM_POST' })).toBe(4);
    expect(withDemo({ earlierSAH: true })).toBe(1);
  });

  it('caps at the top risk bracket', () => {
    const worst = computePhasesScore({
      zones: PT_0041.zones,
      morphology: { maxDiameterMm: 25, aspectRatio: 3 },
      demographics: {
        age: 80,
        hypertension: true,
        earlierSAH: true,
        population: 'Finnish',
        site: 'ACOM_PCOM_POST',
      },
    });
    expect(worst.points).toBe(22); // 5 + 1 + 1 + 10 + 1 + 4
    expect(worst.riskPercent).toBe(17.8);
  });
});

describe('riskFactor — shared by the 2D heatmap and the 3D viewer', () => {
  it('returns 1 at maximum risk and 0 at minimum, for an aneurysm zone', () => {
    expect(riskFactor({ tawss: 0.15, osi: 0, isAneurysm: true }, 'TAWSS')).toBeCloseTo(1, 6);
    expect(riskFactor({ tawss: 1.5, osi: 0, isAneurysm: true }, 'TAWSS')).toBeCloseTo(0, 6);
    expect(riskFactor({ tawss: 1, osi: 0.35, isAneurysm: true }, 'OSI')).toBeCloseTo(1, 6);
    expect(riskFactor({ tawss: 1, osi: 0.03, isAneurysm: true }, 'OSI')).toBeCloseTo(0, 6);
  });

  it('damps non-aneurysm zones to 20%', () => {
    const sac = riskFactor({ tawss: 0.15, osi: 0, isAneurysm: true }, 'TAWSS');
    const parent = riskFactor({ tawss: 0.15, osi: 0, isAneurysm: false }, 'TAWSS');
    expect(parent).toBeCloseTo(sac * 0.2, 6);
  });
});

describe('zone lookup', () => {
  it('is order-independent (the legacy positional indexing bug)', () => {
    const shuffled = [...PT_0041.zones].reverse();
    expect(computeCompositeRisk({ ...PT_0041, zones: shuffled })).toBe(
      computeCompositeRisk(PT_0041),
    );
  });

  it('throws a named error rather than silently producing NaN', () => {
    const noDome = PT_0041.zones.filter((z) => z.id !== 'dome');
    expect(() => computeCompositeRisk({ ...PT_0041, zones: noDome })).toThrow(/dome/);
  });
});

describe('OSI threshold reconciliation (0.3 -> 0.2)', () => {
  it('uses 0.2, per the SAD and the literature', () => {
    expect(THRESHOLDS.OSI_HIGH).toBe(0.2);
  });

  it("PT-2025-0037's dome now alerts where it previously did not", () => {
    const dome = byZone(PT_0037.zones).dome;
    expect(dome.osi).toBe(0.24);
    expect(dome.osi).toBeGreaterThan(THRESHOLDS.OSI_HIGH); // alerts now
    expect(dome.osi).toBeLessThan(0.3); // was silent under the old threshold
  });

  it('leaves the composite-index normalisation range untouched', () => {
    // Changing the alert threshold must NOT move any patient's score.
    expect(computeRiskBreakdown(PT_0037).composite).toBe(62);
  });
});
