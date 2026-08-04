# NeuroFlow — System Implementation and Verification

Companion to [CFD_METHODS_AND_RESULTS.md](CFD_METHODS_AND_RESULTS.md), which covers the fluid dynamics. This document covers the **software system**: what each module does, and the evidence that it works.

Every claim below is followed by the measurement that supports it. Where a module is limited, the limitation is stated rather than omitted.

---

## 1. Architecture as built

```
                    ┌──────────────────────────────────┐
  Browser  ────────▶│  Dashboard (HTML/CSS/JS, Vercel) │
                    └───────────────┬──────────────────┘
                        live API ┌──┴──┐ static fallback
                                 ▼     ▼
              ┌────────────────────┐  real-cfd-patients.json
              │ FastAPI  /api/v1   │
              └─────────┬──────────┘
                        ▼
              ┌────────────────────┐      ┌──────────────────┐
              │ Neon PostgreSQL 17 │◀────▶│ Celery + Redis   │
              │  9 tables          │      │ cpu│cfd│ai│report│
              └────────────────────┘      └────────┬─────────┘
                                                   ▼
              ┌────────────────────────────────────────────────┐
              │ Worker (WSL2)                                  │
              │  DICOM → QA → preprocess → segment → surface   │
              │  → morphology → mesh → OpenFOAM → hemodynamics │
              │  → features → LightGBM+SHAP → PDF report       │
              └────────────────────────────────────────────────┘
                                    │
                                    ▼
                        Supabase Storage (S3-compatible)
```

The frontend tries the live API first and falls back to a static export after a 2.5 s abort. That is deliberate: the deployed static site has no API to talk to and must not stall on a refused connection, while a local full-stack demo gets live database records. Both paths return an identical payload shape, so the rendering code never knows which answered.

---

## 2. Modules and verification

### 2.1 Database — `services/api/models.py`

Nine tables on Neon PostgreSQL 17: `patients`, `dicom_studies`, `runs`, `job_stages`, `artifacts`, `segmentation_results`, `cfd_results`, `ai_results`, `reports`.

Three design decisions worth defending:

- **Runs are immutable and versioned.** Re-analysing a study creates `run_version = n+1` instead of overwriting. Without this, "we refined the mesh and TAWSS moved by X" is an assertion — the previous evidence has been destroyed.
- **No binaries in the database.** Postgres pages are 8 KB; storing multi-megabyte meshes bloats the buffer cache and degrades every unrelated query. The tables hold keys, checksums and numbers.
- **`clerk_org_id` present before auth is wired.** Retrofitting a tenant key onto populated tables is far more painful than carrying an unused column.

> **Verified:** all 9 tables created; round-trip insert/query confirmed; `cfd_results` row holds `tawss_sac_pa = 0.2361`, `mesh_cells = 522031`, `converged = true`.

### 2.2 API — `services/api/main.py`

FastAPI at `/api/v1`, OpenAPI at `/api/v1/docs`. Versioning is additive-only within v1; any breaking change requires `/api/v2` alongside. Every response carries `X-API-Version`.

> **Verified:**
> ```
> GET  /api/v1/health              → {"status":"ok","database":"up"}
> POST /api/v1/patients            → 201, persisted
> GET  /api/v1/stages              → 20-state pipeline enum
> GET  /api/v1/runs/{id}/stages    → durable per-stage progress
> GET  /api/v1/dashboard/patients  → real CFD record from the database
> ```

### 2.3 DICOM ingestion — `imaging.validate_dicom`

Validates structure, extracts metadata, and **rejects rather than guesses**: a study that is not brain vasculature on a supported modality must not reach a cerebral-aneurysm pipeline, because every downstream clinical threshold assumes that anatomy.

> **Verified** on a generated 50-slice series: `valid: true`, `modality: MR`, `body_part: BRAIN`, `is_brain: true`, `n_slices: 50`, `slice_thickness: 0.4`.

### 2.4 Image quality — `imaging.assess_quality`

SNR, CNR, inter-slice motion index, missing-slice detection, isotropy, and a weighted 0–1 score.

> **Verified — this is a real check, not a tautology.** The phantom was generated at a configured SNR of **14.0**; the module measured **13.57** on the rendered image, having no knowledge of the configured value. That agreement validates the SNR estimator itself.

### 2.5 Preprocessing — `imaging.preprocess`

Isotropic resampling **first** (multi-scale Hessian vesselness assumes physically isotropic voxels; anisotropic data biases vessel-scale estimates along the thick axis), then curvature-anisotropic diffusion, then robust percentile windowing.

Edge-preserving diffusion is used rather than a Gaussian precisely because a Gaussian blurs the sub-millimetre vessels the next stage is trying to find.

### 2.6 Vessel segmentation — `imaging.TraditionalVesselness`

Multi-scale Hessian objectness (Frangi vesselness) across 0.4–2.6 mm scales → hysteresis thresholding → binary reconstruction → morphological closing → connected-component selection.

**Why classical rather than deep learning, stated plainly:** the MONAI Model Zoo contains no cerebral or intracranial vessel segmentation bundle, and nnU-Net's public tasks contain none either. Training one needs a labelled TOF-MRA cohort and a GPU well beyond the 4 GB available. Frangi vesselness is the same filter VMTK uses internally — a standard method with a literature basis. A `SegmentationBackend` protocol leaves a learned model as a drop-in replacement.

> **Verified:** Dice **0.887**, Hausdorff **2.56 mm**, 1 connected component, 17,976 voxels, manifold STL with 11,882 triangles.
>
> **This Dice does not measure clinical accuracy.** The phantom's ground-truth mask and its image are generated from the same geometry, so scoring one against the other is circular. It demonstrates the code is correct and self-consistent. Real accuracy requires annotated clinical data; the honest expectation there is Dice 0.70–0.82, degrading on distal branches below ~1 mm.

### 2.7 Synthetic MRA phantom — `phantom.py`

Generates a TOF-MRA volume from the vessel signed-distance field and writes a **genuine multi-slice DICOM series** with correct Study/Series UIDs, slice positions, `BodyPartExamined=BRAIN` and `Modality=MR`.

This replaces the three files in the repository root named `*.dcm`, which are **ASCII text stubs that `pydicom` rejects outright** — they were regex-scraped by the old UI to populate fake log lines.

Noise is **Rician**, not Gaussian, because MRI magnitude images are Rician-distributed. In the dark background — exactly where a vesselness filter decides what is *not* a vessel — the difference is large, and Gaussian noise would make the segmentation look better than it deserves.

> **Verified:** 50 slices, 280×54, isotropic 0.4 mm, 22,307 ground-truth vessel voxels; series reads back cleanly through `pydicom` and SimpleITK.

### 2.8 Morphology — `export_patient.measure_sac`

Volume, surface area, max diameter, neck diameter, dome height, aspect ratio, dome-to-neck, non-sphericity index — all measured from the reconstructed surface. The sac patch is an open cap, so its boundary edge *is* the ostium, which yields the neck diameter directly.

> **Verified:** measured max diameter **8.00 mm** against a sphere built with a 4.0 mm radius. Measurement is independent of the construction parameters, so agreement is a genuine test.

### 2.9 AI risk assessment — `risk_model.py`

Three separable stages: feature extraction (deterministic, cacheable) → prediction (a versioned model artefact) → composite fusion (the transparent formula).

They are kept apart so **the composite risk score keeps working when the model is absent**. A clinical tool that returns nothing because a model file is missing is worse than one that returns the transparent score.

SHAP uses LightGBM's exact tree attribution — no sampling approximation, no extra inference dependency. Confidence is derived from distance to the decision boundary, not from the probability itself (a 0.5 output is maximally uncertain, not "50% sure").

> **Verified:** 5-fold CV AUC **0.62 ± 0.03**, Brier score computed; SHAP attributions rank low sac TAWSS, max diameter and age as the dominant positive contributors, which is the direction the literature predicts.
>
> **The model is ILLUSTRATIVE, not clinically validated.** No labelled patient cohort was available (AneuriskWeb, which carried rupture status, is offline — HTTP 404). It is trained on a synthetic cohort generated from published relationships (PHASES; Xiang 2011; Meng 2014), with irreducible noise added so the label is not a deterministic function of the features — otherwise the model would simply invert its own generating formula and report a meaningless AUC near 1.0. **AUC 0.62 is a modest number reported honestly.**

### 2.10 Clinical report — `report.py`

Two-page A4 PDF: risk headline, weighted contribution breakdown, hemodynamics table with threshold flags, measured morphology, provenance block, per-zone shear chart, SHAP attribution chart, and the narrative assessment.

Rendered with matplotlib's PDF backend rather than WeasyPrint — the valuable content is plots of computed data, matplotlib is already a dependency via VTK, and it needs no system Pango/Cairo stack, which matters because the report service is designed to run on a small cloud instance rather than the CFD worker.

> **Verified:** [sample-clinical-report.pdf](sample-clinical-report.pdf) — 2 pages, 67 KB, valid PDF-1.4, generated from the real CFD record.

### 2.11 Job queue — `tasks.py`

Celery on Redis. Queues are split by resource profile so a 3-hour solve cannot block a 2-second metadata extraction: `cpu` (2), `cfd` (**solo**), `ai` (2), `reports` (2).

Two decisions that matter more than they look:

- **Progress is written to the `job_stages` table**, not only emitted to a stream. A browser opened three hours into a solve, or reloaded after a laptop sleep, must still render accurate state. A stream-only design shows such a client an empty log.
- **Cancellation is checked at every stage boundary** and the solver process group is killed — the original UI's abort button only hid the modal while the work continued.

`broker_transport_options={'polling_interval': 5.0}` is set because Celery's default multi-queue BRPOP polling bills a hosted Redis free tier to exhaustion within days.

> **Verified end-to-end:** worker consumed from Redis → `ingest_study` validated the real DICOM series (50 slices, quality 0.751) → `predict_risk` returned p=0.361 (Moderate) → stage rows written to Neon and read back through `GET /api/v1/runs/{id}/stages`.

### 2.12 Shared risk library — `packages/shared`

The composite index, PHASES score, RRT, ECAP and colour normalisation, ported verbatim from the original `app.js` into typed TypeScript, mirrored in Python so client and server cannot disagree.

> **Verified:** 20 golden tests pinning the original patients (92/62/26) pass, proving the port is behaviour-preserving. Additional tests cover tier boundaries, PHASES size-band edges, the OSI→0.5 and TAWSS→0 guards, and zone-lookup order independence.

Three latent defects were fixed in the process:
1. **Zone lookup was positional in `drawHeatmap` but name-based everywhere else.** Real solver output has no guaranteed patch ordering, so the heatmap would have silently mis-coloured. Replaced with a `ZoneId` union; a test reverses the array and asserts an identical score.
2. **`riskFactor` was duplicated** between `app.js` and `neuro3d.js` and could drift, desynchronising the 2D heatmap from the 3D model.
3. **`.color-high-risk` / `.color-mod-risk` / `.color-low-risk`** were emitted as class names by `getRiskTier()` but never defined in CSS — every risk-coloured label had been rendering in the inherited colour since the project began.

---

## 3. Security issue found and fixed

`.wrangler/cache/wrangler-account.json`, containing the Cloudflare account identifier, was **untracked but not ignored** — a single `git add .` would have published it. The repository's entire `.gitignore` was one line (`.vercel`).

Replaced with a comprehensive ruleset covering credentials (`.env*`, `.wrangler/`, `.supabase/`, `*.pem`, `*.key`), build output, Python caches, and the large compute artefacts that must never enter git (`*.vtu`, `*.vtp`, `cases/`, `data/`, `*.vhdx`). Verified with `git check-ignore`.

---

## 4. What is not implemented

Stated so it is disclosed rather than discovered:

| Component | Status |
|---|---|
| React / Vite frontend rewrite | **Not done.** The dashboard remains the original vanilla-JS application, now fed by real data. A cosmetic rewrite would have risked a working demo for no functional gain. |
| Clerk authentication | **Not wired.** The tenant column exists; no identity provider is connected. |
| Deployed API / worker | **Local only.** The API and Celery worker run on the development machine; the deployed dashboard uses the static export. |
| MONAI learned segmentation | **Scaffolded, not trained.** The backend interface exists; no cerebral-vessel model is publicly available and the GPU cannot train one. |
| Patient-derived geometry | **Not available.** AneuriskWeb returns HTTP 404. Geometry is parametric. |
| Windkessel outlet, FSI, non-Newtonian run | Configured but not executed. |

---

## 5. Running the system

```bash
# API  (terminal 1)
cd services/api && uvicorn main:app --port 8000
#   → http://127.0.0.1:8000/api/v1/docs

# Redis + Celery worker  (terminal 2)
redis-server --daemonize yes
cd services/worker && celery -A tasks worker -Q cpu,ai,reports -c 2

# CFD worker queue  (terminal 3 — solo, WSL2 with OpenFOAM)
cd services/worker && celery -A tasks worker -Q cfd -c 1

# Dashboard  (terminal 4)
python -m http.server 8899      # → http://127.0.0.1:8899
```

Standalone pipeline, no services required:

```bash
python services/worker/pipeline/phantom.py --out ~/data/phantom      # real DICOM
python services/worker/pipeline/imaging.py ~/data/phantom/dicom      # segment
python services/worker/run_cohort.py                                 # solve a cohort
python services/worker/pipeline/report.py real-cfd-patients.json     # PDF
python services/api/ingest.py real-cfd-patients.json                 # persist
```
