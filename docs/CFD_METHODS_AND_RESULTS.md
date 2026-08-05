# NeuroFlow — Computational Methods, Verification and Limitations

**Project:** NeuroFlow CFD Analyst — cerebral aneurysm rupture-risk assessment
**Document purpose:** to state precisely which numbers in this system are *computed*, which are *authored*, how the computed ones were verified, and where the method's accuracy is limited.

This document exists because the project's earlier documentation (§9 of `NeuroFlow_CFD_Project_Documentation.pdf`) correctly disclosed that the segmentation, meshing and solver stages were a scripted visualisation rather than a computation. **That is no longer the case for the hemodynamics.** This document replaces that disclosure with a specific account of what changed.

---

## 1. Summary of what changed

| Stage | Before | Now |
|---|---|---|
| Vessel geometry | none — 2D schematic only | watertight 3D surface, four named regions |
| Volume mesh | none | snappyHexMesh, 522,031 cells, `checkMesh: Mesh OK` |
| Flow solution | none — `setTimeout` animation | OpenFOAM ESI v2412, Navier–Stokes, converged |
| TAWSS / OSI / RRT / ECAP | hardcoded constants in `app.js` | derived from the solved wall-shear-stress field |
| Morphology | hardcoded constants | measured from the reconstructed sac surface |
| Composite Risk Index | real formula, fabricated inputs | real formula, **computed** inputs |

The Composite Risk Index and PHASES score were always genuine calculations. What was missing was real input data. That gap is now closed.

---

## 2. Method

### 2.1 Geometry

A sidewall (saccular) aneurysm is constructed parametrically as the union of a parent artery and a spherical sac:

- parent artery radius 2.0 mm, length 100 mm
- sac radius 4.0 mm, centre offset 1.4 mm above the arterial wall
- 40 mm inlet extension (10 arterial diameters) so a developed profile forms before the flow reaches the sac

The union is evaluated as a **signed distance field**, `d = min(d_cylinder, d_sphere)`, and the surface extracted by marching cubes at the zero level set on a 0.15 mm isotropic grid.

> **Why not constructive solid geometry.** A VTK boolean union of the same two primitives was tried first and rejected by OpenFOAM: `surfaceCheck` reported *"surface is not closed — 11,101 edges not connected to two faces"*, with 13% of triangles below quality 0.05 (minimum 2.6 × 10⁻⁹). snappyHexMesh requires a closed surface to distinguish inside from outside. An SDF union is closed and manifold **by construction**. The final surface reports:
>
> ```
> Surface is closed. All edges connected to two faces.
> Surface has no illegal triangles.
> Triangles: 158,920 in 4 region(s)
> ```
>
> with only 0.7% of triangles below quality 0.05.

A secondary benefit is methodological: real patient geometry also arrives as a segmentation mask processed by marching cubes, so the synthetic case exercises the *same* code path rather than a parallel one.

The surface is written as a single ASCII STL containing four named solids — `inlet`, `outlet`, `wall`, `wall_aneurysm`. snappyHexMesh converts each named solid directly into an OpenFOAM patch, which makes later zone extraction a one-line patch query and removes any dependence on face ordering.

### 2.2 Mesh

`blockMesh` background grid of 361,725 cells (0.4 mm), refined by `snappyHexMesh` with feature-edge snapping and a volume-refinement region around the sac.

```
cells                     522,031
max non-orthogonality        52.3   (limit 65)
max skewness                  3.19  (limit 4)
max aspect ratio              6.45
checkMesh                 Mesh OK
```

### 2.3 Fluid model and boundary conditions

Blood is modelled as an incompressible Newtonian fluid:

| Property | Value |
|---|---|
| density ρ | 1060 kg m⁻³ |
| dynamic viscosity μ | 0.0035 Pa s |
| kinematic viscosity ν = μ/ρ | 3.302 × 10⁻⁶ m² s⁻¹ |

Reynolds number in the parent artery is ≈ 440 (U ≈ 0.37 m s⁻¹, D = 4 mm), comfortably laminar, so no turbulence model is applied. Introducing one at this Reynolds number would add spurious eddy viscosity and corrupt the wall shear stress being measured.

- **Inlet** — `flowRateInletVelocity`. Steady case: 4.6 mL s⁻¹ (mean ICA flow). Pulsatile case: a tabulated ICA waveform over a 0.9 s cardiac cycle (mean 4.6, systolic peak 7.0 mL s⁻¹). Prescribing *flow rate* rather than velocity means the boundary condition remains correct despite the marching-cubes inlet cap not being a perfect analytic disc.
- **Outlet** — traction-free, `p = 0`, `zeroGradient` velocity.
- **Walls** — no slip, rigid.

### 2.4 Solvers

- **Steady:** `simpleFoam`, SIMPLEC, converged in **336 iterations** (residual targets p 10⁻⁵, U 10⁻⁶; final residuals ~10⁻⁷–10⁻⁸).
- **Transient:** `pimpleFoam`, second-order `backward` time scheme, adjustable timestep at Courant ≤ 3, warm-started from the converged steady field so a single cardiac cycle yields a usable cycle average.

> `icoFoam` was rejected: it is fixed-timestep PISO with no rheology hook and no turbulence slot, so it cannot support the Carreau–Yasuda comparison and cannot adapt its timestep to a pulsatile waveform.

### 2.5 Wall shear stress extraction

Three implementation details determine whether the resulting numbers are meaningful. Each is easy to get wrong in a way that still *looks* plausible.

**(a) Units.** OpenFOAM's incompressible solvers are kinematic. The `wallShearStress` function object outputs **m² s⁻², not Pascals**. All values are multiplied by ρ = 1060.
*Failure mode if missed:* every TAWSS reads ~1000× too low, so every case trips the "< 0.4 Pa" low-shear criterion and the dashboard reports universal critical risk with complete confidence.

**(b) TAWSS is the mean of the magnitude, not the magnitude of the mean.**

```
TAWSS = mean(|τ⃗|) · ρ                          ← time-average of the magnitude
OSI   = ½ (1 − |mean(τ⃗)| / mean(|τ⃗|))          ← needs BOTH quantities
```

Applying `fieldAverage` to the wall-shear-stress *vector* yields `|mean(τ⃗)|`, which is the OSI **numerator** — not TAWSS. The two coincide only in perfectly unidirectional flow; inside a recirculating aneurysm sac they differ substantially. The function-object chain therefore computes `mag` **before** `fieldAverage`, and averages both fields.

**(c) Area weighting.** Face areas vary by an order of magnitude after mesh refinement, so all zone statistics are area-weighted rather than arithmetic means.

Derived quantities, with the same guards used on the client side:

```
RRT  = 1 / max(0.02, (1 − 2·OSI) · TAWSS)      guard: OSI → 0.5 singularity
ECAP = OSI / max(0.02, TAWSS)                   guard: TAWSS → 0
NWSS = TAWSS_sac / TAWSS_parent
LSAR = sac area fraction below threshold
```

**LSAR is reported under both definitions.** The literature definition (Xiang et al., 2011) uses *10% of the parent-artery mean*; the project's architecture document implies *< 0.4 Pa absolute*. These diverge whenever parent-artery shear is far from 4 Pa, so collapsing them into a single number would make the metric irreproducible. Both are stored.

**(d) RRT and ECAP are non-linear, so "the average" is ambiguous.** Both are reciprocal in TAWSS, and by Jensen's inequality the surface average of the pointwise value is *not* the value computed from the surface averages. For the sac these differ by more than a factor of two:

| | Sac RRT (Pa⁻¹) |
|---|---|
| Area-weighted mean of per-face RRT | **11.04** |
| RRT evaluated at the mean TAWSS/OSI | **4.24** |

Neither is wrong; they answer different questions. The area-weighted figure is the true spatial average and is dominated by the low-shear regions where RRT is largest. The from-means figure is what a dashboard gauge implies when it displays one TAWSS number and one RRT number derived from it — and it is what the dashboard shows.

Both are computed and stored (`rrt` and `rrt_from_means`, likewise for ECAP). Reporting one while displaying the other is exactly how a system comes to look internally inconsistent while in fact being correct twice.

---

## 3. Verification

### 3.1 Analytic check on wall shear stress

Fully developed laminar flow in a circular pipe has a closed-form wall shear stress:

$$\tau_w = \frac{4\mu Q}{\pi r^3} = \frac{4(0.0035)(4.6\times10^{-6})}{\pi (0.002)^3} = 2.56\ \text{Pa}$$

| | Value |
|---|---|
| Analytic (Poiseuille) | 2.56 Pa |
| Computed (parent artery) | **2.97 Pa** |
| Deviation | **+16%** |

This is the single most important check in the project: it confirms the kinematic → Pascal conversion. Had the ×1060 factor been omitted, the computed value would have been 0.0028 Pa — three orders of magnitude wrong, yet superficially unremarkable on a dashboard.

The +16% residual is explained in §4.1 and is consistent in both sign and magnitude with the known boundary-layer limitation.

### 3.2 Geometric self-consistency

Morphology is measured from the reconstructed surface, not from the parameters used to create it, so agreement is a genuine test of the measurement code:

| Quantity | Expected | Measured |
|---|---|---|
| Sac maximum diameter | 8.00 mm (2 × 4 mm radius) | **8.00 mm** |
| Parent wall area | 1.257 × 10⁻³ m² (2πrL) | 1.227 × 10⁻³ m² (less sac opening) |
| Sac volume | ≤ 268 mm³ (full sphere) | 241 mm³ (partially embedded) |

### 3.3 Physiological plausibility

Aneurysm sac shear is **12.6× lower** than the parent artery (0.236 Pa vs 2.97 Pa), which is the expected consequence of flow stagnation inside a saccular outpouching and the basis of the low-WSS rupture hypothesis (Meng et al.).

### 3.4 Software verification

The risk-scoring logic was ported from the original `app.js` into a typed, tested shared library. **20 golden tests** pin the composite score, tier, PHASES points and RRT/ECAP of the three original demonstration patients to their pre-migration values, so the port is provably behaviour-preserving:

```
PT-2025-0041 → 92 / High     PHASES 7 pts → 2.4%
PT-2025-0037 → 62 / Moderate PHASES 0 pts → 0.4%
PT-2025-0039 → 26 / Low      PHASES 0 pts → 0.4%
```

Additional tests cover tier boundaries, PHASES size-band edges, the OSI → 0.5 and TAWSS → 0 guards, and zone-lookup order independence.

---

## 4. Limitations

These are stated plainly. Each is a real constraint on how far the results can be pushed.

### 4.1 No prism boundary layers on the aneurysm sac

`snappyHexMesh` achieved 3 prism layers over ~69–90% of the parent-artery wall but **0% on the `wall_aneurysm` patch** — the one surface whose TAWSS and OSI are actually reported. Three configurations were tried (varying surface-refinement level, `featureAngle`, `maxThicknessToMedialRatio`, `minThickness`, and volume refinement); all produced 0% coverage on the sac.

Wall shear stress is a wall-normal velocity gradient evaluated in the first cell. Without a boundary layer that gradient is evaluated across a coarser cell, which **systematically overestimates WSS**. The analytic check in §3.1 quantifies this at **≈ +16%** on the parent artery.

*Consequence:* absolute TAWSS values carry roughly a 15–20% upward bias. **Relative** comparisons — sac versus parent, NWSS, LSAR, and the risk tier that follows from them — are far less affected, because the bias acts in the same direction on both surfaces. Absolute values should not be quoted to better than two significant figures.

### 4.2 Geometry is synthetic, not patient-derived

The AneuriskWeb repository (`ecm2.mathcs.emory.edu/aneuriskweb`), the intended source of patient geometry, **returns HTTP 404 — the host is no longer serving**. The IntrA dataset (103 reconstructed brain-vessel models) remains available but is gated behind a registration form and manual Google Drive download.

The geometry used here is therefore **parametric**: a cylinder-plus-sphere sidewall aneurysm with clinically representative dimensions. The physics, solver, mesh and post-processing are real; the anatomy is idealised. No claim is made that these results describe any specific patient.

### 4.3 Model simplifications

- **Rigid walls.** Arterial compliance is neglected; fluid–structure interaction is not modelled.
- **Newtonian rheology.** Blood is shear-thinning. The case is configured for a Carreau–Yasuda comparison (`transportProperties` contains the coefficients, commented) but that comparison has not been run.
- **Single outlet, traction-free.** No Windkessel or resistance outlet model, so the pressure field is relative rather than physiological.
- **One cardiac cycle.** Two or three cycles are the norm; a warm start from the converged steady solution mitigates but does not eliminate start-up transient contamination.

### 4.4 Not implemented

Automatic vessel segmentation from DICOM, the AI rupture-risk model, and the multi-tenant web backend described in the architecture document are **not part of this submission**. The three `.dcm` files in the repository root are ASCII text fixtures, not DICOM, and are rejected by `pydicom`.

### 4.5 Computational budget

The available hardware (6 physical cores, 16 GB RAM, 4 GB GPU) permits roughly one solved case per night. Results are presented for a single geometry; no mesh-independence study or multi-case cohort was performed.

---

## 5. What is real, and what is not

| Component | Status |
|---|---|
| Vessel surface generation (SDF + marching cubes) | **Real** |
| Volume meshing (snappyHexMesh) + quality verification | **Real** |
| Navier–Stokes solution (OpenFOAM) | **Real** |
| WSS → TAWSS / RRT / LSAR / NWSS | **Real** |
| OSI and ECAP | **Real for the pulsatile case** (§6.1). Undefined on a steady solve, and shown as `n/a` rather than 0.00 |
| Morphology measured from the surface | **Real** |
| Composite Risk Index and PHASES score | **Real** (always were). Flagged as a **lower bound** where OSI is uncomputed, since OSI holds 30 % of the weighting |
| DICOM header parsing (`pydicom`) | **Real** |
| 2D heatmap, report generation | **Real** rendering of the above |
| 3D viewer — the **aneurysm sac** | **Real**: positioned at the case's recorded site, sized at true anatomical scale from measured morphology (dome, neck, aspect ratio), coloured by the computed field |
| 3D viewer — the **vessel network around it** | **Generic anatomical asset.** Identical for every patient, not derived from any scan, shown for orientation. The panel states this on screen |
| Aneurysm **anatomy** | **Idealised** — parametric, not patient-derived |
| Automatic vessel segmentation | **Implemented, not exercised on these cases.** `imaging.py` provides DICOM validation, SNR/CNR/motion QA, Frangi/Hessian multi-scale vesselness, region growing and Dice/Hausdorff validation. The cohort geometries are parametric, so nothing was segmented to produce them |
| AI / ML rupture prediction | **Implemented — illustrative only.** LightGBM with exact-tree SHAP, three separable stages (feature extraction → inference → composite). Trained on a **synthetic** cohort from published risk relationships, never on patient data; cross-validated AUC 0.62. Shown on the dashboard and in the report, always with that caveat |
| Cloud backend | **Real and deployed.** FastAPI on Vercel serverless against Neon Postgres — 18 versioned `/api/v1` endpoints (patients, studies, runs, stages, results, reports, dashboard feed), immutable run versioning, `X-API-Version` on every response |
| Auth | **Implemented, not configured on the deployed instance.** Clerk RS256/JWKS verification with organisation-based tenant isolation applied in-query. No Clerk keys are set on the deployment, so it runs unauthenticated — and therefore **all mutating routes refuse with 503**. Reads are open by design: the data is synthetic and the page is meant to be looked at |
| Job queue (Celery/Redis) | **Not implemented.** Runs are executed by the worker directly; the job/stage state machine and its 22 states exist and are served, but nothing brokers them |
| The 6-step upload animation in the UI | **Still a scripted visualisation** |

Cases displaying a **CFD** badge in the dashboard carry a `provenance` block recording solver version, mesh size, mesh quality and convergence. Cases displaying a **DEMO** badge are the original curated dataset and are not computed.

---

## 6. Results

Steady solution, converged in 336 iterations:

| Quantity | Parent artery | Aneurysm sac |
|---|---|---|
| TAWSS | 2.97 Pa | **0.236 Pa** |
| RRT | 0.41 Pa⁻¹ | **11.04 Pa⁻¹** |
| Surface area | 1236 mm² | 176 mm² |

| Derived | Value |
|---|---|
| NWSS (sac / parent) | 0.080 |
| LSAR (relative, < 10% parent) | 84.6% |
| LSAR (absolute, < 0.4 Pa) | 88.2% |
| Composite Risk Index | 49 / 100 — Moderate |

Measured morphology: maximum diameter 8.00 mm, neck 6.87 mm, aspect ratio 0.89, volume 241 mm³, dome-to-neck 1.16.

Clinical criteria triggered: **low TAWSS** (0.236 < 0.4 Pa) and **elevated RRT** (11.04 > 3.0 Pa⁻¹).

OSI is identically zero in the steady solution — correct, not a defect: oscillatory shear requires a time-varying flow. Meaningful OSI comes only from the pulsatile solution.

### 6.1 Oscillatory shear from the pulsatile solve

A steady solve cannot produce OSI. The definition

```
OSI = ½ (1 − |mean(τ⃗)| / mean(|τ⃗|))
```

compares two averages of the same field; with a single flow state they are identical and OSI is exactly 0 **by construction**. ECAP = OSI / TAWSS inherits this. Those zeros are *not measurements of "no oscillation"* and the dashboard no longer displays them as numbers — the gauges read `n/a` with the reason, and because OSI carries 30 % of the Composite Risk Index weighting, a steady case's index is labelled a **lower bound** rather than silently forfeiting 30 % of its score to an unevaluated term.

The pulsatile case (`pimpleFoam`, tabulated ICA waveform, T = 0.9 s) was warm-started from the converged steady field and averaged over t = 0.22–0.808 s — 65 % of the cycle, spanning systolic deceleration through mid-diastole, which is the window in which reversal occurs.

| patch | area (mm²) | TAWSS (Pa) | OSI | ECAP |
|---|---|---|---|---|
| `wall` (parent artery) | 1233.1 | 2.663 | 0.0002 | 0.0001 |
| `wall_aneurysm` (sac) | 172.5 | 0.233 | **0.0096** | **0.0414** |

All values are **area-weighted patch means**, not point samples. The parent artery carries high unidirectional shear with essentially no reversal; the sac has 11× lower shear with 48× the OSI and 414× the ECAP. That is the expected signature of recirculation in a sidewall aneurysm, and it is the project's first genuinely computed OSI.

Stated plainly, because it matters for interpretation: **0.0096 is small**. It sits below the 0.2 clinical alert threshold and below the 0.03 floor of the risk normalisation, so a real OSI barely moves the Composite Risk Index for this geometry. Its value is that the quantity is now measured rather than assumed — which is precisely what the lower-bound framing predicted. A sac with stronger recirculation (higher aspect ratio, narrower neck) would be expected to produce substantially more.

Two honest limitations on this number:

- **One cycle, warm-started.** Convention is to average over the last of two or three cycles. A warm start removes most of the start-up transient, but a single cycle remains the weaker choice and cycle-to-cycle variation is unquantified here.
- **The window stops at 0.808 s, not 0.90 s.** Late diastole is excluded. Flow there is slow and near-steady, so its contribution to reversal is small, but it is not zero.

---

## 7. Reproducing these results

```bash
# 1. Generate the tagged vessel surface
python services/worker/pipeline/geometry.py \
    --out ~/cases/mycase/constant/triSurface --name vessel

# 2. Mesh and solve  (blockMesh -> snappyHexMesh -> checkMesh -> pimpleFoam)
bash services/worker/openfoam/run_case.sh ~/cases/mycase 6

# 3. Extract hemodynamics
python services/worker/pipeline/hemodynamics.py ~/cases/mycase

# 4. Export as a dashboard patient record
python services/worker/pipeline/export_patient.py ~/cases/mycase \
    --id PT-2026-0101 --out real-cfd-patients.json
```

Environment: OpenFOAM ESI **v2412**, Python 3.12 with VTK 9.6.2 / PyVista 0.48.4, on WSL2 Ubuntu 24.04.

> **Note on MPI:** WSL2 exposes logical CPUs while OpenMPI counts physical cores, so `mpirun` must be given `--use-hwthread-cpus` or it will refuse `-np 6` with a "not enough slots" error. Long solves must also run as foreground processes inside a persistent session — a job backgrounded with `nohup … &` inside `wsl -e bash -lc` is killed when the launching `wsl.exe` exits, regardless of `nohup`.
