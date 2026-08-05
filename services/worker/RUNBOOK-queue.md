# Running the job queue

Celery + Redis, four queues split by resource profile.

## Why four queues

| queue | work | concurrency | why |
|---|---|---|---|
| `cpu` | DICOM ingest, segmentation | 2 | short, I/O- and CPU-bound |
| `cfd` | OpenFOAM solve | **1 (solo)** | a six-core MPI solve must not contend with itself |
| `ai` | feature extraction, LightGBM inference | 2 | fast, CPU-light |
| `reports` | PDF generation | 2 | needs neither OpenFOAM nor the GPU |

The split exists because these have genuinely different profiles. Running a
3-hour solve on the same worker as a 2-second PDF means the PDF waits 3 hours.
`cfd` runs solo because `mpirun -np 6` already saturates the machine — a second
concurrent solve would halve both.

## Local

```bash
redis-server --daemonize yes --save "" --appendonly no

source ~/.venvs/neuroflow/bin/activate
cd services/worker

# One worker across all queues, for development.
celery -A tasks worker --loglevel=info -Q cpu,cfd,ai,reports --concurrency=1

# Or split them, which is what production wants:
celery -A tasks worker -Q cfd     --concurrency=1 -n cfd@%h &
celery -A tasks worker -Q cpu,ai  --concurrency=2 -n cpu@%h &
celery -A tasks worker -Q reports --concurrency=2 -n rep@%h &
```

Check it is consuming:

```bash
celery -A tasks inspect active_queues
redis-cli llen cfd          # depth of the solve queue
```

## Environment

| variable | used by | notes |
|---|---|---|
| `REDIS_URL` | API + worker | **Must be identical on both.** Without it the API reports `queue.enabled = false` and runs stay QUEUED |
| `DATABASE_URL` | API + worker | the worker writes progress into `job_stages` directly |
| `FOAM_CASE_ROOT` | API | where case directories live; defaults to `~/cases` |
| `FOAM_NPROC` | API | MPI ranks for a solve; defaults to 6 |

Celery cannot use Upstash's REST API — it needs the wire protocol:
`rediss://default:<password>@<host>:6379`.

## The deployment constraint, stated plainly

The API runs on Vercel serverless: 30 s maximum, no OpenFOAM binary, no MPI, no
persistent process. **It can enqueue but can never execute.** A solve needs
hours and six cores.

So a working production deployment needs all three of:

1. a broker both sides can reach (Upstash, or Redis on any host),
2. a worker on real hardware — the WSL machine, or a VM with OpenFOAM,
3. the same `DATABASE_URL` on both, since progress travels through `job_stages`
   rather than through Celery's result backend.

With only (1) configured, jobs queue and nothing consumes them. `/api/v1/health`
reports `queue.enabled` so that state is visible rather than silent.

## Why progress lives in the database, not in Celery

`job_stages` is the durable record; the API's dispatcher deliberately has **no
result backend**. A four-hour job outlives any number of client connections, and
Celery's result store is ephemeral — a broker restart loses it. A browser opened
three hours late reads the same progress from Postgres that one opened at the
start would see.

## Cancellation

`POST /api/v1/runs/{id}/cancel` sets the run to `CANCELLING`. The worker checks
that flag at stage boundaries and kills the `mpirun` process group. It is not
instantaneous: a solve is interrupted at the next boundary, not mid-timestep.
