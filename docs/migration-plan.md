# Neuro-Flow — Migration to the SAD V3 Hybrid Architecture

> ## STATUS — 2026-08-04
>
> This is the **planning** document. For what was actually built, measured and
> verified, see **[CFD_METHODS_AND_RESULTS.md](CFD_METHODS_AND_RESULTS.md)**.
>
> **Delivered:**
> - Phase 0 / 0.5 — environment relocated to D:, OpenFOAM ESI v2412, VMTK 1.5.0,
>   worker venv, five cloud services authenticated, Supabase storage provisioned
>   and round-trip verified.
> - Phase 3 (partial) — `packages/shared` risk library ported verbatim from
>   `app.js` with **20 golden tests** pinning the original patients' scores.
> - **Spike 2A + 2C-2 PASSED** — real geometry → snappyHexMesh (522k cells,
>   `Mesh OK`) → OpenFOAM → hemodynamics. Parent artery 2.97 Pa, aneurysm sac
>   0.236 Pa, verified against the analytic Poiseuille solution (2.56 Pa, +16%).
> - Computed cases wired into the dashboard with provenance badges; deployed.
>
> **Deviations from this plan:**
> - **Object storage: Cloudflare R2 → Supabase** (R2 activation requires a
>   payment method). Wrapped behind a `StorageBackend` interface — see the
>   "Object storage" section below.
> - **Geometry: AneuriskWeb → parametric.** The Emory host returns HTTP 404;
>   the repository is gone. Fallback 2 from Phase 2C was taken.
> - **Prism layers on the aneurysm sac: unresolved** after three configurations.
>   Quantified at ≈ +16% WSS overestimate and documented as a limitation rather
>   than hidden.
>
> **Not built:** React/Vite rewrite, FastAPI gateway, Clerk auth, Celery/Redis
> queue, report service, AI risk model, automatic segmentation. The dashboard
> remains the original vanilla-JS application, now fed by real CFD output.

## Context

`d:\fyp` currently holds **NeuroFlow CFD Analyst**: 4 vanilla files (`index.html`, `app.js`, `style.css`, `neuro3d.js`), no build step, no backend, patient data as an in-memory object literal. Its own documentation PDF §9 states plainly that segmentation, meshing and the Navier–Stokes solver are *scripted animation*, and lists real CFD as Future Work.

**SAD V3** specifies a far larger system: React+Vite+TS → Clerk → FastAPI/Render → Neon Postgres → Cloudflare R2 → Celery/Redis → SimVascular + OpenFOAM + segmentation workers, across ~20 modules.

Decisions taken: **full vertical slice** · **real SimVascular + OpenFOAM CFD** · **traditional segmentation first, MONAI later** · Windows + WSL2 + NVIDIA · **1–2 weeks**.

### Three verified facts that shape the plan

| Fact | Measured | Consequence |
|---|---|---|
| **C: free space** | **1.9 GB of 139.9 GB** | WSL `ext4.vhdx` and Docker's disk both sit on C:. WSL's "954 GB available" is a sparse-file illusion capped by the real 1.9 GB. **Resolution: relocate to D: (61.4 GB free)** — Phase 0. |
| **GPU** | Quadro P1000, **4 GB**, Pascal sm_61 | No tensor cores; nnU-Net `3d_fullres` OOMs. **Training a segmentation model here is not possible in 2 weeks** — which is exactly why traditional-first is the right call, not a compromise. CUDA→WSL passthrough works (`nvidia-smi` responds). |
| **CPU / RAM** | i7-8850H, **6 physical cores**; 15.8 GB, WSL capped at 7 GB | ~**one CFD case per night**. Expect **2–3 solved cases by day 14**, not ten. |

**Target by day 14:** a deployed React dashboard showing real TAWSS/OSI from a real OpenFOAM solve on a real aneurysm geometry, persisted in Neon with versioned artifacts in object storage, for 2–3 cases.

---

## Phase 0 — Relocate everything to D: (day 0 evening, ~3h). **Gate for everything.**

D: has **61.4 GB free**. Budget: WSL base ~9 · OpenFOAM ~3 · SimVascular ~2 · Python/ITK/VTK stack ~4 · datasets ~5 · CFD output ~10–25 → **~35–50 GB**. Only works with the `purgeWrite` / wall-patch-only discipline in Phase 2C; without it one case eats 16 GB.

**The nuance that decides fast-vs-unusable:** put the **WSL virtual disk** on D:, and let cases live *inside* it on **native ext4**. Never run cases from `/mnt/d` — WSL reaches Windows drives over 9p, **10–50× slower** on the many-small-files I/O that `decomposePar` and time-directory writes generate. Same physical drive, double the solve time.

1. Move the distro — **verify the export before unregistering**:
   ```
   wsl --shutdown
   wsl --export Ubuntu D:\wsl\ubuntu-backup.tar
   wsl --unregister Ubuntu
   wsl --import Ubuntu D:\wsl\Ubuntu D:\wsl\ubuntu-backup.tar --version 2
   ```
   Restore the default user in `/etc/wsl.conf` (`[user] default=<name>`) or you land as root.
2. Docker Desktop → Resources → Advanced → **Disk image location `D:\docker`**, then `docker system prune -a --volumes`. *(Not on the critical path — OpenFOAM installs natively via apt.)*
3. `C:\Users\hp\.wslconfig`: `memory=11GB`, `processors=10`, `swap=8GB`, `swapFile=D:\\wsl\\swap.vhdx`.
4. **Still reclaim some C:** — 1.9 GB free risks failed Windows updates and page-file stalls regardless. Target ≥ 15 GB, floor 8 GB.
5. Layout: repo stays `D:\fyp` (Vite/React dev runs natively on Windows, fine there). Worker uses `FOAM_CASE_ROOT=~/cases` — **ext4 inside the vdisk, physically on D:**.
6. **Request ADAM dataset access now** (registration-gated, takes days).
7. **Viva insurance, 10 min:** `git tag legacy-static && git push`, deploy today's 4 files to a second Vercel project `fyp-legacy`. A working demo URL no matter what.

**GO:** rootfs backed by `D:\wsl\Ubuntu\ext4.vhdx` · `free -g` ≥ 10 · D: ≥ 45 GB · C: ≥ 8 GB.
**NO-GO:** if D: tightens once datasets land, move meshing+solving to a cloud CPU instance — you keep real CFD, you lose the "local worker" story. **Decide day 0.**

### Standing policy: **D: drive for everything in this project**

Steps 1–3 above cover the big items. Auditing the rest of the plan surfaced **four remaining leaks onto C:** — close them on day 0 or they refill the system drive silently over two weeks.

```powershell
# npm global installs + cache (currently C:\Users\hp\AppData\Roaming\npm)
npm config set prefix "D:\dev\npm-global"
npm config set cache  "D:\dev\npm-cache"
# then add D:\dev\npm-global to PATH (User env var)

# pip cache (currently C:\Users\hp\AppData\Local\pip)
pip config set global.cache-dir "D:\dev\pip-cache"
```

> **Gotcha before you run the npm one:** `@anthropic-ai/claude-code` and `altimate-code` are installed at the *old* prefix. Changing it orphans them from `PATH`. Either keep both prefixes on `PATH`, or reinstall the two globals after switching. Do this **before** installing pnpm/vercel/wrangler/neonctl so they land on D: from the start.

Everything else already resolves to D: — the WSL vdisk (and therefore OpenFOAM, `~/.venvs/neuroflow`, the micromamba VMTK env, `~/cases`, and all datasets), the Docker disk, and the repo at `D:\fyp` with its `node_modules`.

**One unavoidable exception:** `.wslconfig` must sit at `C:\Users\hp\.wslconfig` — Windows reads it only from `%USERPROFILE%`. It's a ~100-byte text file whose *contents* point everything at D:, so the exception costs nothing.

**Also relocate:** this plan file itself, and any future planning docs, belong in `D:\fyp\docs\` under version control — not in `C:\Users\hp\.claude\plans\`.

---

## Phase 0.5 — Environment audit & provisioning (day 0, after the disk move)

### Audited state (measured 2026-08-03)

| Component | Windows | WSL2 Ubuntu 24.04.4 |
|---|---|---|
| node / npm | ✅ 25.2.1 / 11.6.2 | ❌ absent (not needed there) |
| pnpm · vercel · wrangler · neonctl | ❌ **all four missing** | — |
| python | ✅ 3.12.9, pip 24.3.1 | ✅ 3.12.3, **pip3 missing** |
| git · gh | ✅ 2.52 / 2.96, **authenticated as `Muhammad-Hamza69`** | ✅ 2.43 |
| docker · wsl | ✅ 29.1.3 / 2.6.3 | — |
| build toolchain | — | ❌ **no `gcc`/`g++`/`make`/`cmake`** |
| MPI | — | ❌ no `mpirun` |
| **OpenFOAM** | — | ❌ **not installed**, no apt repo added |
| **SimVascular** | — | ❌ **not installed** |
| conda / micromamba | — | ❌ absent (needed for the VMTK py3.9 env) |
| redis-cli · psql | — | ❌ absent |
| GPU | Quadro P1000 4 GB, driver 573.71 | ✅ visible via `nvidia-smi`; **no CUDA toolkit** |
| Python libs | `pydicom 3.0.2`, `numpy 2.5.0`, `scipy 1.18`, `boto3`, `Jinja2` | ❌ **nothing** |

**Headline: WSL is effectively a pristine 1.9 GB distro.** Every piece of the compute stack has to be installed from zero — which is *why* Phase 0 is a hard gate, not a nicety: the vdisk sits on a C: drive with 1.9 GB free, so **right now you cannot install OpenFOAM at all** (it needs ~3 GB).

### Five findings that change the plan

1. **`gh` token lacks the `workflow` scope.** Scopes are `gist, read:org, repo`. The SAD calls for GitHub Actions CI, and **pushing any file under `.github/workflows/` will be rejected** with this token. Fix before day 1: `gh auth refresh -h github.com -s workflow` *(interactive — opens a browser)*.
2. **Ubuntu 24.04 enforces PEP 668.** System-wide `pip install` fails with `externally-managed-environment`. **All Python work must live in a venv** (`~/.venvs/neuroflow`); the worker's systemd/Celery entrypoint must reference that interpreter explicitly. Silently working around this with `--break-system-packages` will corrupt the distro's own Python.
3. **SimVascular has no official Ubuntu 24.04 build** — releases target 18.04/20.04/22.04 and pull older Qt5/libpng dependencies. **This is the one new integration risk the audit surfaced.** Fallbacks, in order: (a) install the 22.04 `.deb` and satisfy deps manually; (b) run SimVascular in a **Docker container** — Docker 29.1.3 is already installed and this is the cleanest isolation; (c) register a **second WSL distro, Ubuntu 22.04**, purely for SimVascular; (d) drop back to PyVista capping and let VMTK supply centerlines, losing only the GUI model-prep. **Time-box this to 2 hours on day 1** — if it isn't running by then, take (d) and move on; the four-face surface is achievable either way.
4. **No CUDA toolkit, and that's fine.** PyTorch's `cu121` wheels bundle their own runtime — `nvcc` is only needed to compile custom kernels, which we don't. Driver 573.71 + WSL passthrough is sufficient. **Do not install the 3 GB CUDA toolkit**; on a 61 GB budget that's pure waste.
5. **numpy 2.5.0 on the Windows side.** VTK/SimpleITK/MONAI wheels are numpy-2 compatible now, but pin `numpy>=2.1,<3` in the worker venv and let pip resolve — don't inherit the Windows install's version by accident. The WSL venv is independent, so this is a lockfile discipline issue, not a blocker.

### Install runbook (ordered; step 0 is mandatory first)

**0. Phase 0 disk relocation must be complete.** Verify `df -h /` shows the vdisk on D: and C: has ≥ 8 GB before proceeding.

**1. Windows CLIs** (~2 min):
```bash
npm i -g pnpm@9 vercel@latest wrangler@4 neonctl@2
gh auth refresh -h github.com -s workflow          # interactive, opens browser
```

**2. WSL base toolchain** (~5 min, ~1.5 GB):
```bash
sudo apt update && sudo apt install -y \
  build-essential cmake pkg-config git curl wget unzip \
  python3-pip python3-venv python3-dev \
  redis-tools postgresql-client \
  libgl1 libxrender1 libxcursor1 libxinerama1 libxft2 \
  libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0   # WeasyPrint
```

**3. OpenFOAM ESI v2412** (~3 GB) — brings its own OpenMPI, which is where `mpirun` comes from:
```bash
curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash
sudo apt install -y openfoam2412-default
echo 'source /usr/lib/openfoam/openfoam2412/etc/bashrc' >> ~/.bashrc
# verify:
source ~/.bashrc && simpleFoam -help >/dev/null && echo OK
```

**4. Worker Python venv** (~2.5 GB) — PEP 668 makes this non-optional:
```bash
python3 -m venv ~/.venvs/neuroflow && source ~/.venvs/neuroflow/bin/activate
pip install -U pip wheel
pip install "numpy>=2.1,<3" scipy pandas \
  pydicom SimpleITK itk vtk pyvista trimesh scikit-image opencv-python-headless \
  scikit-learn lightgbm shap \
  "celery[redis]" redis sqlalchemy alembic "psycopg[binary]" boto3 \
  fastapi uvicorn pydantic-settings python-dotenv PyFoam monai
```
`monai` installs without torch — correct for the traditional-first path; MONAI's transforms and `DiceMetric`/`HausdorffDistanceMetric` work CPU-only.

**5. VMTK in an isolated py3.9 env** (~1.5 GB) — **VMTK does not install on Python 3.12**, so the morphology stage shells out to this interpreter:
```bash
curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj bin/micromamba
./bin/micromamba create -y -n vmtk -c conda-forge python=3.9 vmtk
```

**6. SimVascular** — download the Ubuntu `.deb` from `simvascular.github.io`; **time-boxed, see finding 3**.

**7. Deferred to when MONAI v2 is actually attempted** (~2.5 GB — don't install on day 1):
```bash
pip install torch --index-url https://download.pytorch.org/whl/cu121   # cu121 supports Pascal sm_61
```

**8. Cloud services** — accounts exist for Neon, Clerk, Vercel, GitHub. **Supabase needs a free signup (GitHub OAuth, no card)** for storage. Note **Clerk has no CLI** — dashboard-only (details in Phase 4).

**Disk after all of the above: ~11 GB**, leaving ~50 GB on D: for datasets and CFD output. Within budget, but only with the `purgeWrite` discipline in Phase 2C.

**Verification gate:** `simpleFoam -help` succeeds · `python -c "import vtk, pyvista, SimpleITK, itk, monai, celery, sqlalchemy, boto3"` clean in the venv · `micromamba run -n vmtk vmtkcenterlines --help` succeeds · `gh auth status` lists `workflow` · `pnpm -v && vercel -v && wrangler -v && neonctl --version` all respond · **`npm config get prefix` and `npm config get cache` both return D: paths** · `wsl -d Ubuntu -e df -h /` shows the vdisk on D: · C: free space **unchanged or higher** than before the install run.

---

## Phase 1 — Pipeline architecture (the shape everything else follows)

### Stage order — morphology now sits **before** CFD

Morphology is derived from the reconstructed surface and has no dependency on the solve, so it runs earlier. This matters practically: **morphology + PHASES alone produce a usable risk score in ~2 minutes**, so the dashboard shows real numbers long before a 3-hour solve finishes — and if CFD fails, the case still yields a defensible partial result.

```
VALIDATING → PREPROCESSING → SEGMENTING → SEG_VALIDATING → RECONSTRUCTING
  → MORPHOLOGY ──────────────────────────────┐   (partial risk available here)
  → MODEL_PREP (SimVascular) → MESHING → MESH_QC → BOUNDARY_SETUP
  → SOLVING → POSTPROCESSING → THRESHOLD_EVAL                 │
  → FEATURE_EXTRACTION ← ─────────────────────┴───────────────┘
  → RISK_PREDICTION → COMPOSITE_RISK → REPORTING → SUCCEEDED
Terminal: FAILED | CANCELLING → CANCELLED
```

### Module ownership (maps 1:1 onto SAD modules)

| Stage | Module | Tech | Queue |
|---|---|---|---|
| VALIDATING | DICOM validation + metadata + modality check | pydicom, SimpleITK | `cpu` |
| PREPROCESSING | QA (SNR/CNR/motion) + Gaussian/median/CLAHE | SimpleITK, OpenCV | `cpu` |
| SEGMENTING | **v1 traditional** (see 2B) | ITK, SimpleITK, scikit-image | `cpu` |
| SEG_VALIDATING | Dice/Hausdorff, topology, CCA | MONAI metrics, SciPy | `cpu` |
| RECONSTRUCTING | Marching cubes → smooth → decimate → STL/OBJ | VTK, PyVista | `cpu` |
| **MORPHOLOGY** | volume, AR, dome-to-neck, ostium, NSI, UI, tortuosity | VMTK (py3.9 env), PyVista | `cpu` |
| MODEL_PREP | **SimVascular**: capping, named faces, centerlines, BC prep | SimVascular Python API | `cfd` |
| MESHING / MESH_QC | snappyHexMesh + prism layers; `checkMesh` parse | OpenFOAM | `cfd` |
| BOUNDARY_SETUP | ρ/μ, Newtonian + Carreau–Yasuda, pulsatile inlet, outlet splits | OpenFOAM dicts | `cfd` |
| SOLVING | `pimpleFoam` transient N-S + live residuals | OpenFOAM, PyFoam | `cfd` |
| POSTPROCESSING | WSS→TAWSS/OSI/RRT/LSAR/NWSS/ECAP | PyVista, NumPy | `cfd` |
| THRESHOLD_EVAL | clinical benchmarks, flagged zones | NumPy, pandas | `cpu` |
| **FEATURE_EXTRACTION** | assemble + normalize geometric ⊕ hemodynamic ⊕ clinical vector | pandas, scikit-learn | `ai` |
| **RISK_PREDICTION** | LightGBM inference + confidence + SHAP | LightGBM, SHAP | `ai` |
| **COMPOSITE_RISK** | weighted fusion of CFD + morphology + clinical + ML → tier | shared risk lib | `ai` |
| REPORTING | **separate service** (see below) | WeasyPrint, Jinja2 | `reports` |

**Why AI is three modules, not one:** feature extraction is deterministic and cacheable; risk prediction is a swappable model artifact with its own version; composite risk is the transparent, defensible weighted formula from [app.js:103-118](app.js#L103-L118) that must keep working **even when the ML model is absent**. Collapsing them would make the honest fallback impossible.

### SimVascular's role — it replaces work, it doesn't just add a step

SimVascular handles **model construction, cap generation, named face tagging, centerline extraction, and inflow/outlet BC prep** — replacing hand-rolled PyVista capping. Its named caps export **is** the four-patch surface (`inlet`, `outlet`, `wall`, `wall_aneurysm`) that makes zone extraction a one-line patch query mapping straight onto the frontend's `ZoneId` union.

**Integration seam, stated honestly:** SimVascular has no first-class OpenFOAM exporter. Two paths — **committed:** export the clean capped surface with named faces → volume-mesh in **snappyHexMesh** (hex-dominant, materially better wall-shear accuracy, and `addLayers` gives the prism layers WSS *requires*). **Alternative:** SimVascular/TetGen volume mesh → `.msh` → `gmshToFoam` (tets, weaker for WSS). Take the first.

Runs headless in WSL2 via its bundled Python API; WSLg provides the GUI on Win11 when you need to eyeball a model.

**Bonus validation story:** SimVascular ships `svSolver`. Running one case through both svSolver and OpenFOAM gives a genuine cross-solver TAWSS comparison — high-value, low-cost. **Stretch, not committed.**

### Progress tracking (first-class, not a UI afterthought)

A `job_stages` table — one row per (job, stage) with `state`, `progress 0..1`, `started_at`, `ended_at`, `message`, `metrics jsonb`. The worker updates it at every stage boundary and on meaningful sub-progress (mesh %, solve time/total, iteration count). It is the **durable** source of truth; the WebSocket is a live view over it. Consequence: a browser opened three hours late still renders the exact same progress bar, because it reads the table, not a stream tail.

### Artifact versioning (immutable, never overwritten)

Every job is a **run** with a monotonic `version` per study. Re-running never clobbers prior output — essential when you re-solve at a finer mesh and need to compare, and it's what makes "we changed the rheology and TAWSS moved by X" a defensible claim.

`artifacts` table: `artifact_id, study_id, run_version, stage, kind, storage_key, bytes, sha256, created_at`. `kind ∈ {dicom, mask, surface_stl, surface_obj, morphology_json, mesh, checkmesh_log, wall_vtp, residuals_csv, hemo_json, features_json, prediction_json, shap_json, report_pdf, figure_png}`.

### Object storage — `StorageBackend` interface, Supabase Storage as the implementation

**Decision (2026-08-04, changed from the SAD):** Cloudflare R2 requires a payment method to activate (`wrangler` fails with `code 10042`, and there is no CLI path — activation is a billing subscription, dashboard-only). We are not putting a card on file for this project, so storage moves to **Supabase Storage**.

**Why Supabase over the alternatives:** it exposes an **S3-compatible endpoint**, so the worker's `boto3` client, presigned URLs, and multipart uploads work **unchanged** from the R2 design — the swap costs one env-var block, not a rewrite. Vercel Blob would need no new signup (the account already exists) but is not S3-compatible and is JS-first, which is poor fit for a Python worker.

**Wrap it behind an interface anyway** — `services/worker/storage/backends/{s3.py, local.py}` implementing `put/get/presign/delete/exists`, selected by `STORAGE_BACKEND`. Same pattern as `SegmentationBackend`. Two payoffs: `cli.py` runs a full case with `local` and zero network, and if R2 is ever enabled it becomes a config change.

```
neuroflow-data/                                  # private bucket, presigned access only
  orgs/{clerk_org_id}/patients/{patient_id}/studies/{study_id}/
    runs/v{n}/
      segmentation/  surface/  morphology/
      mesh/          cfd/      hemodynamics/
      risk/          report/figures/
neuroflow-assets/                                # public bucket
  models/  fonts/  report-templates/
```

**The 1 GB constraint and the policy that handles it.** Supabase free tier is **1 GB storage / 5 GB egress per month**, against R2's 10 GB. A single TOF-MRA series is 50–150 MB, so three cases of raw DICOM alone would consume half the quota. Therefore:

- **Large raw inputs stay local.** Raw DICOM lives on the WSL worker at `~/data/raw/`, referenced in Postgres by local path plus SHA-256. It is never uploaded — nothing in the UI needs the raw voxels, only derived artifacts.
- **Only browser-facing derived artifacts go to cloud storage**: surface STL (5–20 MB), wall `.vtp` (~2 MB), report PDF, figure PNGs, morphology/risk JSON. Three full cases land at roughly 100–150 MB — comfortably inside 1 GB.
- **CFD volume output never leaves the worker** (it never entered git either). `purgeWrite 5` still applies.
- `raw/` therefore disappears from the bucket layout above; the DB keeps `local_path` alongside `storage_key` on the artifact row.

Rules otherwise unchanged: org prefix first (tenant isolation maps to a policy prefix boundary later) · everything under `runs/v{n}/` · artifacts immutable, re-running bumps `run_version`.

**Honest note for the report:** the SAD specifies R2 with its zero-egress advantage. Record this as a deliberate, cost-driven deviation with the interface boundary that makes it reversible — that is a stronger engineering story than silently shipping something the architecture doc doesn't match.

### Report Service — separate, and on Render not the laptop

Reporting moves to its own service consuming a `reports` queue. Two concrete reasons: WeasyPrint is CPU-light and needs neither OpenFOAM nor the GPU, so **it runs on Render and doesn't depend on your laptop being awake**; and report regeneration (new template, re-sign, re-export) must not queue behind a 3-hour solve. It reads artifacts from object storage by `(study_id, run_version)` and writes `report_pdf` back — no coupling to the CFD worker beyond the DB and bucket.

### Queues

| Queue | Worker | Concurrency | Why |
|---|---|---|---|
| `cpu` | WSL | 2 | Short I/O- and CPU-bound stages |
| `cfd` | WSL | **1 (solo)** | A 6-core solve must not contend with itself |
| `ai` | WSL | 2 | Feature extraction + inference |
| `reports` | **Render** | 2 | Decoupled from the laptop |

---

## Phase 2 — De-risking spikes (days 1–3). No FastAPI until 2C and 2B pass.

### 2A — SimVascular model prep (day 1 AM)

Install the Linux build in WSL2. Load one **AneuriskWeb** case (`ecm2.mathcs.emory.edu/aneuriskweb/`) — each ships a reconstructed `surface/model.stl` plus ground-truth morphology **and rupture status**, which is what makes Phase 4's ML module a real experiment rather than a hand-wave. Clip inlet/outlets to flat planes, cap, tag the four faces, extract centerlines, add a ~10-diameter cylindrical inlet extension so a plug profile develops before reaching the sac.

> **GATE 2A:** a watertight, four-face-named surface exports and loads in PyVista with the expected patch names.
> **Fallback:** PyVista-only capping (the original approach) — costs the centerlines, which VMTK then supplies in the morphology stage anyway.

### 2B — Segmentation v1: traditional (days 1, 3)

**Traditional first is the right engineering call here, not a concession** — no pretrained cerebral-vessel model exists in the MONAI Model Zoo or nnU-Net's public tasks, and a 4 GB Pascal card can't train one. Stating that plainly in the report is stronger than a hand-wave.

**v1 (committed):**
1. Preprocess: `Spacingd` resample → percentile intensity scaling → skull-region crop
2. **ITK `MultiScaleHessianBasedMeasureImageFilter` + `HessianToObjectnessMeasureImageFilter`** (Frangi vesselness — the same filter VMTK uses internally), multi-scale over expected vessel radii
3. Adaptive/hysteresis threshold → **region growing** seeded from the ICA → `KeepLargestConnectedComponent` → `FillHoles`
4. Marching cubes → `vtkWindowedSincPolyDataFilter` → `vtkQuadricDecimation` → STL
5. **Validate for real:** `DiceMetric` + `HausdorffDistanceMetric` against ground truth

**Honest expectation: Dice 0.70–0.82** on the Circle of Willis and the sac; poor on distal branches under ~1 mm. That number goes in the report, replacing the fabricated "98.4% confidence" currently printed by [neuroflow-local-pipeline/pipeline.py](neuroflow-local-pipeline/pipeline.py).

**v2 (MONAI, later — post-day-14 or stretch):** the module exposes a `SegmentationBackend` interface with `backend: "traditional" | "monai"` on the job request, so v2 slots in without touching any other stage. Realistic v2 = fine-tuning a small `monai.networks.nets.UNet` at **96³ patches, fp32** (Pascal's fp16 runs 1:64, so AMP is *counterproductive* on a P1000) on ADAM or on v1 pseudo-labels.

**Data:** ADAM (254 labeled TOF-MRA, gated — request day 0) → TubeTK/Bullitt (100 annotated, immediate, CC) → IXI (unlabeled, QA module only).

> **GATE 2B (end day 3):** vesselness on a real TOF-MRA yields a connected CoW mask whose STL SimVascular/snappyHexMesh accepts.
> **Fallback:** hand-segment one case in **3D Slicer** (~2 h) to unblock CFD; keep the automatic path as a separately-reported deliverable with its honest Dice. Label the pipeline "semi-automatic" — a legitimate clinical-research posture.

### 2C — Real OpenFOAM (days 1–2, highest risk)

**Install OpenFOAM ESI v2412**, not Foundation v12/13 — the Foundation `foamRun -solver` rename invalidates every tutorial and forum answer you'll need under time pressure.
```bash
curl https://dl.openfoam.com/add-debian-repo.sh | sudo bash && sudo apt install openfoam2412-default
```

**Mesh: `snappyHexMesh`**, 3 prism layers, 0.4–0.8M cells.
> **GATE 2C-1 (end day 1):** `checkMesh` → Mesh OK, non-orthogonality < 65, skewness < 4, layer coverage > 60% on wall patches.
> **Fallbacks:** `cfMesh cartesianMesh` → or an idealized cylinder+sphere sidewall aneurysm via `blockMesh`+snappy (still real Navier–Stokes and real WSS; only the geometry is synthetic — document it as such).

**Solver: `pimpleFoam`, not `icoFoam`.** icoFoam is fixed-dt PISO with no rheology hook and no turbulence slot — it physically cannot do Carreau–Yasuda. pimpleFoam gives adjustable dt (`maxCo`), laminar `momentumTransport`, and `viscosityModel BirdCarreau` (ESI's `BirdCarreau` includes the `a` exponent, i.e. it *is* Carreau–Yasuda).

- Warm-start `simpleFoam` → pulsatile pimpleFoam (halves transient settling)
- **Inlet:** `flowRateInletVelocity` with a tabulated ICA waveform (Ford 2005 / Hoi 2010; T = 0.9 s, mean ≈ 4.6 mL/s). With the inlet extension this beats a hand-rolled Womersley BC on both fidelity-per-effort and defensibility.
- **Outlets:** `zeroGradient U` + `p=0` primary; Murray's-law (r³) splits on secondaries. True 3-element Windkessel needs `codedMixed` — **stretch**.
- ν = 0.0035/1060 = **3.302e-6 m²/s**, reusing the constants already in [neuroflow-local-pipeline/src/config.py](neuroflow-local-pipeline/src/config.py)
- **Run both rheologies** — a Newtonian vs Carreau–Yasuda TAWSS comparison is a genuinely publishable FYP result for one extra overnight run

**Three traps that silently produce garbage:**
1. **Units.** Incompressible OpenFOAM is kinematic — `wallShearStress` outputs **m²/s², not Pa**. **Multiply by ρ = 1060.** Miss this and every TAWSS is ~1000× too small and every alert fires.
2. **`fieldAverage` gives the wrong half of OSI.** Averaging the WSS *vector* yields `|mean(τ⃗)|` — the OSI **numerator**. TAWSS is `mean(|τ⃗|)`. Chain `wallShearStress` → `mag` → `fieldAverage` over **both** fields, `timeStart` at the **last cardiac cycle only**.
3. **Disk.** Volume fields 100×/cycle on 1M cells ≈ 80 MB/write → 16 GB/case. `purgeWrite 5` for volume fields; write **wall-patch-only** `vtkWrite` scoped to `(wall wall_aneurysm)` for samples, ~2 MB each.

**Derived metrics** (PyVista over the wall `.vtp`): `TAWSS = magWallShearStressMean × 1060` · `OSI = 0.5(1 − mag(mean τ⃗)/mean|τ⃗|)` · `RRT`/`ECAP` **identical to [app.js:137-146](app.js#L137-L146)** · `NWSS` = sac mean ÷ parent mean · **`LSAR` — compute both**: literature (Xiang 2011) is *sac area fraction below 10% of parent mean*, the SAD implies *< 0.4 Pa absolute*; store `lsar_relative` and `lsar_absolute`, report the relative, note the reconciliation. Zone values are **area-weighted patch means**, never point samples.

> **GATE 2C-2 (end day 2) — the make-or-break.** One constant-inlet cycle completes; PyVista reads the wall `.vtp`; TAWSS after ×ρ lands in **0.1–8 Pa** with the sac visibly below the parent artery.
> **Fallback:** ship **one precomputed case** as a labelled fixture and wire the full stack to it. Real CFD numbers survive; on-demand solving does not.

**Runtime, 6 cores, 6-way `decomposePar`:** coarse 0.4M/2 cycles = **2–4 h (committed)** · standard 0.8M = 6–14 h · fine 1.5–2M/3 cycles = 24–40 h (stretch). **One case per night.**

**Results → cloud is simpler than it looks:** the Celery worker *runs inside WSL2*, so the process that `subprocess.run(["mpirun", ...])` also does `boto3`→object storage and SQLAlchemy→Neon. No cross-boundary transfer, no tunnel — outbound connections only.

---

## Phase 3 — Repo restructure + data contract (days 1–2)

```
fyp-project/
├── pnpm-workspace.yaml            # pnpm@9
├── apps/web/                      # Vite 6 + React 19 + TS 5.7 → Vercel
├── services/
│   ├── api/                       # FastAPI 0.115 + SQLAlchemy 2 + alembic → Render
│   ├── worker/                    # Celery 5.4, queues cpu|cfd|ai → WSL2
│   │   ├── pipeline/{dicom,qc,preprocess,segment,segval,recon,morphology,
│   │   │             modelprep,mesh,meshqc,boundary,cfd,hemo,threshold,
│   │   │             features,predict,composite}.py
│   │   ├── segment/backends/{traditional.py, monai.py}   # swappable
│   │   ├── openfoam/case_template/{0,constant,system}/
│   │   └── cli.py                 # same argparse UX as today's pipeline.py
│   └── reports/                   # separate service, queue `reports` → Render
├── packages/shared/src/{risk.ts, thresholds.ts, zones.ts, api.d.ts}
├── infra/{sql/, scripts/, r2-cors.json, render.yaml}
└── docs/                          # all PDFs/DOCX/legacy .md land here
```

**File mapping:**
- **[app.js:99-209](app.js#L99-L209) → `packages/shared/src/risk.ts`, verbatim + typed.** The crown jewel. **Write vitest golden tests pinning the three existing patients' composite scores *before* touching anything** — that makes the migration provably lossless. Mirror it in `services/worker/pipeline/composite.py` with the same fixtures, so client and server can never disagree.
- [app.js:30-96](app.js#L30-L96) → `apps/web/src/mocks/patients.ts` (MSW) **and** `infra/sql/002_seed.sql`
- **[app.js:217-310](app.js#L217-L310) (118 module-level `getElementById`) → deleted**
- `drawHeatmap`/`getInterpolatedColor`/`lerpColor` → pure `(ctx, patient, mode, hover) => void` behind `useHeatmapCanvas`. **Keep canvas; don't rewrite as SVG.**
- **`runCfdSimulation` ([app.js:973-1439](app.js#L973-L1439)) → deleted as simulation, but harvest its four canvas animations.** They become views of **real** data: convergence plots real PyFoam residuals off the WebSocket; the waveform plots your actual `flowRateInletVelocity` Q(t); the mesh panel renders real `checkMesh` stats. Same drawing code, real source — **the highest-value reuse in the migration**, and the demo looks identical but is now honest.
- [style.css](style.css) → one global `app.css`. **Given 14 days, do not refactor to CSS Modules.** Three mandatory edits only (Phase 5).
- [neuro3d.js](neuro3d.js) → `features/viewer3d/{NeuroViewer.tsx, ViewerCore.ts}`, class in a `useRef`. `window.NeuroViewer` and `window.__neuroPendingRiskUpdate` die. `three@0.171` from npm; drop the CDN importmap; Font Awesome → `lucide-react`.
- `neuroflow-local-pipeline/` → `services/worker/`. [src/dicom_parser.py](neuroflow-local-pipeline/src/dicom_parser.py) is the only real code there — keep and extend. **Keep `cli.py`'s argparse UX**; running a case with no Redis is the debugging path that survives when the cloud doesn't.

**The 35 MB GLB — retire it.** It's a *nerve* asset unrelated to any patient's vasculature, and [neuro3d.js:132](neuro3d.js#L132) picks it by traversal order, which is brittle. Once segmentation emits real per-patient STLs the viewer loads **those** from object storage. Transitionally serve it from `neuroflow-assets` behind `VITE_ASSET_BASE_URL` — **not** `apps/web/public/`, which Vite copies verbatim and re-uploads 35 MB per build. `git rm --cached`; **don't rewrite history** — a 36 MB repo is annoying; history surgery two weeks from a deadline is pure downside risk.

**Vercel:** reuse project `fyp` (`prj_Afd7y0gCrupvBWadr9GDUV5xxGYi`) to keep the URL cited in the approval letter. Root Directory `apps/web`, build `pnpm build`, output `dist`. Move `.vercel/` into `apps/web/`; delete the root `.vercelignore`.

**`.gitignore` currently contains only `.vercel`.** Expand *before* any `.env` exists: `node_modules/ dist/ .vercel/ .venv/ __pycache__/ data/ cases/ *.foam .env .env.* !.env.example *.vhdx *.tar`.

### API versioning policy

All routes under **`/api/v1`**. Within v1, **additive only** — new optional fields and new endpoints are fine; renaming, removing, or narrowing a field requires `/api/v2` running side by side. Every response carries `X-API-Version`. The OpenAPI schema is committed and **CI fails on an undeclared breaking diff**. Frontend pins `packages/shared/src/api.d.ts`, regenerated via `pnpm gen:api` from the live schema. Same discipline for artifacts: `run_version` in the path means an old report always resolves against the exact inputs that produced it.

### REST surface

```
GET  /api/v1/health · /me
CRUD /api/v1/patients · /patients/{id}
POST /api/v1/patients/{id}/studies/upload-url     → {study_id, upload_url, storage_key}
POST /api/v1/patients/{id}/studies/{sid}/complete → validate, extract metadata, quality_score
GET  /api/v1/studies/{sid} · /studies/{sid}/runs
POST /api/v1/studies/{sid}/runs                   → {run_version, job_id}
     body:{segmentation_backend:"traditional"|"monai", mesh_preset, cycles, rheology, stages?}
GET  /api/v1/jobs/{jid} · /jobs/{jid}/stages · POST /jobs/{jid}/cancel
GET  /api/v1/jobs/{jid}/logs?after_seq=
GET  /api/v1/studies/{sid}/runs/{v}/{segmentation|morphology|mesh|cfd|hemodynamics|features|prediction|risk}
GET  /api/v1/studies/{sid}/runs/{v}/artifacts     → [{kind, storage_key, bytes, sha256}]
GET  /api/v1/artifacts/{aid}/download-url
POST /api/v1/studies/{sid}/runs/{v}/report        → enqueued on `reports`
GET  /api/v1/reports/{rid}
POST /api/v1/internal/jobs/{jid}/{events,results,stages}   # worker→api, HMAC, not Clerk
```

**WebSocket `/api/v1/ws/jobs/{job_id}`** — pass the Clerk JWT via **`Sec-WebSocket-Protocol`**, not a query string (query strings land in access logs). Events: `state | stage | log | metric | residual | artifact | done`, each with monotonic `seq`.

Two non-obvious requirements:
- **Redis Stream `job:{id}:events` (`XADD`/`XRANGE`), not pub/sub.** A 4-hour job outlives dozens of WS connections; without replay-from-`seq` the UI blanks after every reconnect, sleep, or Render restart.
- **Throttle residuals to 1 msg/s.** pimpleFoam emits per-timestep; ~20k steps × 4 fields would burn Upstash's 500k free tier in one run.

### Shared types

```ts
type ZoneId = "inlet" | "outlet" | "neck" | "dome";   // kills the positional coupling
interface Zone { id:ZoneId; label:string; patch:string; tawss:number; osi:number;
                 rrt:number; ecap:number; area_mm2:number; is_aneurysm:boolean }
interface Morphology { max_diameter_mm:number; aspect_ratio:number; volume_mm3:number;
                       dome_to_neck:number; ostium_area_mm2:number;
                       undulation_index:number; non_sphericity_index:number; tortuosity:number }
interface RiskResult { composite:number; tier:"Low"|"Moderate"|"High";
                       breakdown:{tawss,osi,diameter,aspect};
                       phases:{items:{label,value,points}[]; points:number; risk_percent:number};
                       ml?:{probability:number; confidence:number; model_version:string;
                            shap:{feature,value}[]};
                       completeness:"morphology_only"|"full" }
```
`completeness` is what lets the dashboard honestly show a partial score at the MORPHOLOGY stage. Ship `byZone(zones)` in `zones.ts`; **nothing may index `zones[0..3]` or match `z.name === "Aneurysm Dome"` again.**

**The parallelism enabler:** implement the whole contract in **MSW v2**, seeded with the three legacy patients plus a scripted WS replay derived from the old `runCfdSimulation` timings. The frontend reaches completion by day 5 with zero backend while the worker codes against the same Pydantic models. **The single most important decoupling decision in the plan.**

Neon schema = SAD tables + `runs`, `job_stages`, `artifacts`, `models`; `clerk_org_id` tenant isolation throughout.

---

## Phase 4 — Provisioning

`vercel`, `neonctl`, `wrangler` are **not installed**; `gh` 2.96, node 25.2.1, python 3.12.9, docker 29.1.3 are.

```bash
npm i -g vercel@latest neonctl@2 wrangler@4 pnpm@9

gh auth status                                   # remote: Muhammad-Hamza69/fyp-project
git tag legacy-static && git push origin legacy-static && git checkout -b feat/monorepo

neonctl auth
neonctl projects create --name neuro-flow --region-id aws-ap-southeast-1   # match Render's region
neonctl connection-string dev --pooled           # → Render DATABASE_URL
neonctl connection-string dev                    # → worker (direct: long transactions)

# Supabase Storage (replaces R2 — see "Object storage" above)
# Signup: https://supabase.com  — GitHub OAuth, free, NO card required
# Then: Project Settings -> Storage -> S3 Connection -> generate access key + secret
#   endpoint: https://<project-ref>.supabase.co/storage/v1/s3   region: us-east-1
# Buckets are created via dashboard or the S3 API (boto3 create_bucket):
#   neuroflow-data   (private)
#   neuroflow-assets (public)

cd apps/web && vercel link --project fyp --scope team_wR7UcRn2gnDQcvxAXU1KUNhl
# then set Root Directory = apps/web in project settings
```

**No usable CLI — dashboard only (don't lose an hour discovering this):**
- **Supabase S3 keys.** Dashboard → Project Settings → Storage → S3 Connection → generate access key + secret. No card required.
- **Clerk.** No CLI for creating applications. Create app → Email + Google → **enable Organizations** (the SAD's `clerk_org_id` multi-tenancy depends on it). Backend validates via `clerk-backend-api`.
- **Upstash Redis.** **Celery cannot use the REST API** — it needs the wire protocol: `rediss://default:<pass>@<host>:6379`.
- **Render.** Push `infra/render.yaml` (api + reports worker), then dashboard → New → Blueprint.

**Two tier calls I'd make firmly:** **Render Starter ($7/mo), not Free** — free spins down after 15 min with a ~50 s cold start, which breaks the WebSocket job tracking you're demoing. And Celery `broker_transport_options={'polling_interval': 5.0}` with **explicit queue routing** — default multi-queue BRPOP polling burns Upstash's free tier in days.

**Secrets:** `VITE_*` on Vercel is **baked into the bundle and world-readable** — never put `CLERK_SECRET_KEY`, storage keys, or `DATABASE_URL` there. Render gets pooled `DATABASE_URL`, Clerk secret, storage creds, `REDIS_URL`, `WORKER_HMAC_SECRET`. Worker `.env` (chmod 600, gitignored) gets the **direct** `DATABASE_URL`, `FOAM_CASE_ROOT=~/cases`, `FOAM_NPROC=6`.

---

## Phase 5 — Migration correctness checklist

1. **[index.html:234](index.html#L234) has a raw `<`** — `Low TAWSS (< 0.4 Pa)`. HTML tolerates it; **JSX/Babel throws a parse error.** Only occurrence. *(The div nesting in 233–334 is actually balanced — verified by tag-stack parse. The indentation misleads; validate JSX with a parser, not by eye.)*
2. **`zones` dual coupling.** `drawHeatmap` reads `colors[0..3]` positionally ([app.js:469-472](app.js#L469-L472)) while four sites match `z.name === "Aneurysm Dome"`. Real CFD output has no guaranteed ordering — the heatmap would silently mis-color. `ZoneId` + `byZone()`; SimVascular's named faces make this natural.
3. **StrictMode double-mount / WebGL.** [neuro3d.js](neuro3d.js) has module-level `initialized`/`loadStarted`, an uncancelled rAF, a never-removed resize listener, zero `.dispose()`. Under React 19 StrictMode: **two WebGL contexts and two rAF loops on a 4 GB GPU with a 450k-vertex mesh** — near-certain context loss on this hardware. `useRef`-owned instance + full cleanup (`cancelAnimationFrame`, `removeEventListener`, `renderer.dispose()`, `forceContextLoss()`, per-geometry/material dispose).
4. **Abort that actually aborts.** [app.js:956-959](app.js#L956-L959) only hides the modal — the `await sleep()` chain completes and four `setInterval`s keep drawing to hidden canvases. New: cancel → `CANCELLING` → worker checks a Redis flag at stage boundaries **and `os.killpg`s the `mpirun` process group**. Client: `AbortController`, `ws.close()`, every ported interval cleared in cleanup.
5. **OSI threshold 0.2 vs 0.3.** App uses `>0.3` ([app.js:633](app.js#L633), 808, 853; [index.html:246](index.html#L246)); SAD says `>0.2`. **Adopt 0.2**, but **do not touch the normalization range** `(osi-0.03)/(0.35-0.03)` at [app.js:108](app.js#L108) — a separately calibrated scale; changing it moves all three patients' scores. Centralize in `thresholds.ts`. **This changes the demo:** PT-2025-0037's dome OSI 0.24 was silent, now alerts. Update its `clinicalAssessment` and golden fixtures; record it as a deliberate reconciliation.
6. **`.color-high-risk`/`.color-mod-risk`/`.color-low-risk` don't exist in CSS** — only the same-named custom properties. `getRiskTier().riskLabelClass` has emitted dead classnames all along. Add rules, or better: return a token and style via `[data-tier="high"]`.
7. **[style.css:1983-1993](style.css#L1983-L1993) is orphaned invalid CSS** after the `@media print` close. PostCSS may error or silently drop it. Delete.
8. **Zero responsive breakpoints** against `grid-template-columns: 280px 1fr 340px` ([style.css:138](style.css#L138)). Unusable under ~1300px — non-negotiable before a viva on unknown hardware. Add 1280px and 900px.
9. **The three root `.dcm` files are ASCII stubs, not DICOM** — `pydicom.dcmread` rejects all three. They break the moment parsing becomes real. **Sequence the swap for day 3** using `create_mock_dicom.py` (which does emit genuine DICOM) plus a real ADAM/AneuriskWeb series.
10. **Duplicated color logic** — `getInterpolatedColor` and [neuro3d.js:37-46](neuro3d.js#L37-L46) implement the same normalization with a stale `>0.3` comment. Unify into one `riskFactor(zone, mode)` in shared.
11. Smaller: keep `window.print()` as fallback until the report service ships; replace blocking `alert()`/`confirm()` with toasts.

---

## 14-day schedule

Scope grew (SimVascular, three AI modules, separate report service, versioning). **What gives is CFD case count, not architecture** — plan on 2–3 solved cases.

| Day | Deliverable | Status |
|---|---|---|
| **D0** eve | Phase 0 relocation to D: → **Phase 0.5 install runbook steps 1–5** (~11 GB, mostly unattended). `gh auth refresh -s workflow`. ADAM request. `legacy-static` tag + `fyp-legacy` deploy. | **GATE** |
| **D1** | AM: SimVascular (**2h time-box**, fallback (d) if it resists 24.04); AneuriskWeb case; four-face model; snappyHexMesh. PM: monorepo, Vite/TS scaffold, `shared/risk.ts` **+ golden tests**. | **2A + 2C-1 GATES** |
| **D2** | simpleFoam → pimpleFoam 1 cycle; `wallShearStress`→`mag`→`fieldAverage`; TAWSS ×1060 verified. Frontend: MSW implementing the full v1 contract. | **2C-2 GATE** |
| **D3** | FastAPI `/api/v1` + alembic (incl. `runs`/`job_stages`/`artifacts`) + Clerk + presigned storage + Neon → Render. Traditional segmentation → STL. Overnight: first pulsatile run. | **2B GATE** |
| **D4** | Celery + Upstash, four queues; Redis Stream events; `job_stages` progress tracking end-to-end; artifact versioning + storage key scheme. | Committed |
| **D5** | React dashboard port: heatmap, gauges, 3D viewer, report modal, live job panel wired to the WS. | Committed |
| **D6** | **VERTICAL SLICE** — one real case end-to-end: real STL and versioned artifacts in object storage, real TAWSS/OSI in Neon, real numbers on the deployed dashboard. | **The milestone that matters** |
| **D7** | Buffer. *(Assume one day of slip; you will use it.)* | — |
| **D8** | **Morphology module before CFD**: VMTK centerlines in a separate py3.9 conda env, subprocess-invoked (**VMTK does not install on py3.12** — budget an hour here, not D1) → tortuosity, dome-to-neck, ostium, NSI, UI. Partial-risk path (`completeness:"morphology_only"`). Mesh QC from real `checkMesh`. | Committed |
| **D9** | Segmentation validation (real Dice/Hausdorff). Image QA (SNR/CNR/motion). Overnight: case 2. | Committed |
| **D10** | **Feature Extraction** + **Risk Prediction** (LightGBM on ~100 AneuriskWeb cases with rupture labels, LOOCV, SHAP, versioned model artifact) + **Composite Risk Engine** as three separate modules. **Report AUC honestly (expect 0.65–0.75)**, framed as illustrative. | Committed |
| **D11** | **Report Service** on Render: `reports` queue, WeasyPrint + Jinja2 + PyVista figures → versioned `report_pdf` → object storage → signed download. | Committed |
| **D12** | Case 3 overnight. Responsive breakpoints. Audit logging. Real cancel. Storage retention policy. | Committed |
| **D13** | WS reconnect/replay, StrictMode/WebGL disposal, pytest + vitest, OpenAPI diff check in CI, seed/demo mode. | Committed |
| **D14** | Freeze, deploy, demo video, **"what is real vs simulated" appendix**. | Committed |

**Stretch:** MONAI segmentation backend v2 · svSolver cross-validation · Windkessel outlets · fine mesh 3-cycle · GLSL streamlines · clipping planes · VTK.js.

**What is still simulated at D14 if spikes fail — state it plainly either way:** 2C-2 fails → one precomputed case ships as a labelled fixture (stack real, on-demand solving not). 2B fails → segmentation is manual-in-Slicer, pipeline is "semi-automatic". Regardless: 2–3 cases solved; the ML model is illustrative, not clinically validated; outlets are a resistance approximation, not true Windkessel; MONAI is scaffolded behind the backend interface but not trained.

---

## Verification

**Gates (stop-the-line):** Phase 0 disk targets · four-face watertight model from SimVascular · `checkMesh` OK / non-orth < 65 / skew < 4 · TAWSS in 0.1–8 Pa with sac < parent · traditional-segmentation STL accepted downstream.

**Automated:**
- `pnpm vitest` — golden tests pinning PT-2025-0041/0037/0039 composite scores to pre-migration values (with the documented OSI-threshold delta), plus `byZone`, `riskFactor`, PHASES boundaries.
- `pytest services/worker` — `dicom_parser` against a real `create_mock_dicom.py` file; **hemodynamics against an analytic Poiseuille case where WSS = 4μQ/πr³ is known in closed form** (the single best check that the ×1060 conversion is right); Dice/Hausdorff against a stored mask; **`composite.py` against the same fixtures as `risk.ts`** so client and server provably agree.
- `pytest services/api` — Clerk rejection, HMAC rejection on `/internal/*`, presigned round-trip, job state-machine transitions, **artifact immutability (re-running bumps `run_version`, never overwrites)**.
- CI: `alembic upgrade head && downgrade base` clean · **OpenAPI diff fails on undeclared breaking change**.

**End-to-end (day 6, repeated day 14):**
1. Deploy; sign in via Clerk with an Organization.
2. Create a patient, upload a real DICOM series → confirm raw DICOM is registered locally with its SHA-256, and derived artifacts land under `orgs/{org}/patients/{pid}/studies/{sid}/runs/v1/`.
3. `POST /studies/{sid}/runs` with `mesh_preset=coarse` → watch the WS: stage transitions, real PyFoam residuals on the convergence canvas, real `checkMesh` stats.
4. **At MORPHOLOGY, confirm the dashboard already shows a partial risk score** with `completeness:"morphology_only"` — before the solve finishes.
5. Kill the tab mid-solve, reopen → **stages and logs replay from the DB and `seq`** (proves `job_stages` + Redis Stream, not pub/sub).
6. Cancel → confirm `pimpleFoam` is actually dead (`pgrep -f pimpleFoam` empty).
7. On success: cross-check the dashboard's TAWSS/OSI against `cfd_results` **and** a manual ParaView read of the wall `.vtp`. **All three must agree.**
8. Re-run the same study → confirm `run_version` 2 appears, v1 artifacts untouched, and both are independently fetchable.
9. Request a report → confirm it renders on the **Render** worker (stop the WSL worker first to prove decoupling), lands under `runs/v{n}/report/`, and the signed URL downloads.
10. Load the deployed URL at 1024px and confirm the layout is usable.

**Manual smoke:** `cd services/worker && python cli.py --case <dir> --stages all` runs a full case with no Redis, no API, no network.
