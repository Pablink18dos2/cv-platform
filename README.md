# CV Platform

A local computer-vision platform for building video-analysis prototypes at zero cloud cost.
A **project** connects a video source to a detection model and one or more business analyzers,
and every project shares the same web front end.

All inference runs locally. No GPU and no paid service account are needed.

## Course project

This is the final project for **AI Dev Tools Zoomcamp 2026** (DataTalksClub).

- Task tracker: the GitHub issues of this repository. Each issue has acceptance criteria and is
  the source of truth for what to build.
- Agent roles (orchestrator, PM, engineer, QA) and the task lifecycle: [`_docs/process.md`](_docs/process.md).
- Course evaluation checklist with the current status of each criterion:
  [`_docs/checklist-proyecto-final.md`](_docs/checklist-proyecto-final.md).

> Note: the detailed documents in `_docs/` (architecture, API, performance, testing guidelines,
> design system, ...) are written in Spanish. The key documents are in English: this README,
> [`AGENTS.md`](AGENTS.md), [`_docs/plan.md`](_docs/plan.md), [`_docs/process.md`](_docs/process.md)
> and the course checklist.

## The problem

Analyzing live video (a street, a parking lot, a process camera) usually ends up as a throwaway
script per use case: one to count cars, another to look for free parking spaces, each with its
own way of reading the video and storing results. By the third case nothing is reused.

Here the reusable piece is the **platform**: connectors, models, analyzers and sinks are
interchangeable blocks behind a contract. Adding a new use case means writing an analyzer and a
YAML file, not a new project.

## What it does

- Connects interchangeable video sources: file, YouTube live stream, RTSP, webcam.
- Detects objects with YOLO11 and gives them stable identities with multi-object tracking.
- Runs analyzers on those detections. The first two are **traffic flow** (directional line
  crossings) and **parking occupancy** (free spaces per zone).
- Stores events and per-minute aggregated metrics.
- Serves a web dashboard with the list of projects and, for each one, the annotated video, its
  KPIs and its time series.

What it does **not** do is listed in [`_docs/plan.md`](_docs/plan.md#non-goals) and is just as
important. In short: no license plates, no identification of people, no model training.

## Architecture

Two separate processes, on purpose:

```
Source -> Detector -> Tracker -> Analyzers -> Sinks -> SQLite / Postgres
(connector) (YOLO11)  (ByteTrack)                  \-> last annotated frame (JPEG on disk)
                                                          |
                                         FastAPI (read-only) -> React dashboard
```

- **Worker** (`cvp run <project>`): one per project. Runs the whole pipeline above and is the
  only CPU-heavy process.
- **API** (`cvp api`): read-only over the database and the last-frame file. It loads no models.
- **Blocks**: `VideoSource`, `Detector`, `Tracker`, `Analyzer` and `Sink` are each a `Protocol`
  in `cvp.core`; implementations are registered by name and blocks talk to each other only
  through those contracts. A project is a `project.yaml` (plus a `zones.json` with the polygons).
- **Stack**: Python 3.13 + uv, ultralytics (YOLO11) with ByteTrack, supervision, OpenCV +
  yt-dlp + ffmpeg, FastAPI, SQLModel (SQLite locally, Postgres in Docker), React + Vite +
  Tailwind. Rationale and rejected alternatives: [`_docs/plan.md`](_docs/plan.md#tech-stack).

Details of each block and its contract: [`_docs/architecture.md`](_docs/architecture.md) (Spanish).

## Quick start

### With Docker (one command)

Requirements: Docker with Compose v2. Nothing else: no Python, Node or ffmpeg on the machine.

```bash
docker compose up --build          # runs projects/01-parking-trafico by default
```

The first `--build` takes a few minutes (CPU-only torch is ~180 MB instead of ~2.5 GB for
CUDA); later runs reuse the cache. To use the demo project, which plays a video stored in the
repo and needs no network for the source:

```bash
PROJECT=projects/00-demo docker compose up --build             # bash
$env:PROJECT = "projects/00-demo"; docker compose up --build   # PowerShell
```

Once the worker has been writing for a few seconds, the dashboard is at
<http://localhost:8080> and the API (with its `/docs`) at <http://localhost:8000>.

| Service | What it is |
|---|---|
| `db` | Postgres 17. The API creates the schema on startup |
| `api` | `cvp api`, the read-only API |
| `worker` | `cvp run $PROJECT`: one worker, one project |
| `web` | nginx: serves the built dashboard and proxies `/api` to the API |

`api` and `worker` share one image. Volumes are `data` (the last annotated frame of each
project), `models` (the YOLO weights, which the worker downloads on first use: that first time
does need network access) and `pgdata` (the database). `projects/` is mounted from the repo, so
editing a `project.yaml` and restarting the worker is enough, no rebuild.

Variables, all optional: `PROJECT` (default `projects/01-parking-trafico`), `WEB_PORT` (8080),
`API_PORT` (8000), `POSTGRES_PASSWORD` (`cvp`; the database is not published outside the
Compose network) and `CVP_LOG_LEVEL`. Compose sets `DATABASE_URL` to point at Postgres (with the
`postgresql+psycopg://` driver); the `project.yaml` files read it in the `sqlite` sink and fall
back to SQLite when it is undefined, as in local runs.

```bash
docker compose logs -f worker      # the pipeline health line
docker compose down                # removes the containers; volumes are kept
docker compose down -v             # also removes data, weights and database
```

### Locally

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), ffmpeg on the PATH and, for the
dashboard, Node.

```bash
uv sync                                          # install dependencies into .venv
uv run cvp --version                             # the CLI responds
uv run cvp run projects/00-demo --max-frames 150 # worker: process the demo video (SQLite sink)
uv run cvp api                                   # read-only API on http://localhost:8000
cd frontend && npm ci && npm run dev             # dashboard on http://localhost:5173
```

The dashboard origin must be listed in `CVP_CORS_ORIGINS`; see `frontend/README.md` (Spanish).

## Tests

One command runs the whole backend suite:

```bash
uv run pytest
```

Tests marked `network` are excluded by default (they reach the internet). CI runs the fast
subset, which needs neither network nor model weights, plus lint and the frontend tests:

```bash
uv run pytest -m "not slow and not network"
uv run ruff check . && uv run ruff format --check .      # lint and format
cd frontend && npm test                                   # frontend tests (Vitest)
```

Tests are split into `unit` (fast), `component` (pipeline with test doubles) and
`integration` (real model). See [`_docs/testing-guidelines.md`](_docs/testing-guidelines.md)
(Spanish) for what is tested and how.

## Project layout

```
backend/src/cvp/   the package: core, sources, detectors, trackers, analyzers, sinks, pipeline, api
projects/          one directory per project: project.yaml + zones.json
frontend/          React dashboard shared by all projects
docker/            backend and frontend Dockerfiles and the nginx config (see compose.yaml)
tests/             unit (fast) / component (pipeline with doubles) / integration (real model)
_docs/             specification, architecture and working guides
```

## Documentation

| Document | Purpose | Language |
|---|---|---|
| [`_docs/plan.md`](_docs/plan.md) | Product spec: what is built, for whom, and what is out of scope | English |
| [`_docs/process.md`](_docs/process.md) | How work is organized: agent roles and task lifecycle | English |
| [`AGENTS.md`](AGENTS.md) | Durable context for coding agents | English |
| [`_docs/checklist-proyecto-final.md`](_docs/checklist-proyecto-final.md) | Course evaluation checklist and current status | English |
| [`_docs/architecture.md`](_docs/architecture.md) | The blocks, their contracts and how to add a new one | Spanish |
| [`_docs/api.md`](_docs/api.md) | The read-only API | Spanish |
| [`_docs/testing-guidelines.md`](_docs/testing-guidelines.md) | What is tested and how | Spanish |
| [`_docs/design-system.md`](_docs/design-system.md) | UI design system | Spanish |
| [`_docs/rendimiento.md`](_docs/rendimiento.md) | Performance measurements | Spanish |

## Deployment

Live demo: <https://cv-platform-mrg8.onrender.com> (the free service sleeps when idle; the first request can take about a minute).


The public demo runs for free on [Render](https://render.com) (free web service, Docker runtime:
512 MB RAM, 0.1 CPU). The full image (torch + YOLO) does not fit in 512 MB, so Render runs a
**light version**: the real dashboard and API plus **recorded data**, with no detector and no
worker. The data is a real 30-minute run of `01-parking-trafico` (2026-10-24), exported to
[`deploy/seed.sqlite3`](deploy/seed.sqlite3) by [`scripts/make_seed.py`](scripts/make_seed.py).
[`scripts/replay_seed.py`](scripts/replay_seed.py) replays it in a loop: on start it loads the
recorded minutes up to now, and every minute it inserts the next one with the current timestamp.
**This is replayed recorded data, not live inference.** The snapshot is the last real annotated
frame ([`deploy/seed-preview.jpg`](deploy/seed-preview.jpg)).

If `deploy/demo/` exists (`clip.mp4` with the detections already drawn + `timeline.json`), nginx serves
it at `/demo/` (with range requests, so the video can be scrubbed) and the project view shows a
**replay panel** instead of the image: the looped recorded clip with play/pause, a scrub bar with
marks at every change in free spaces, and charts synced to the video position. Where
`/demo/timeline.json` is a 404 (every other environment) the view is unchanged.

```
Render $PORT -> nginx -> dashboard (Vite build) + /api -> uvicorn (cvp api) -> SQLite (/app/data)
                                                           ^ replay_seed.py inserts one minute / min
```

One image, [`docker/render.Dockerfile`](docker/render.Dockerfile) (about 430 MB on disk, about
80 MB of RAM at runtime), described by the [`render.yaml`](render.yaml) blueprint. The full image
with the detector is the one in `compose.yaml` (`docker compose up --build`).

- **Automatic**: [`deploy.yml`](.github/workflows/deploy.yml) calls the Render deploy hook with the
  commit SHA when the CI workflow succeeds on `main`, behind the `production` environment, and then
  smoke-tests `/api/v1/projects`. Auto-deploy on push is off in `render.yaml`, so the hook is the
  only trigger.
- **Rollback**: *Run workflow* in the Actions tab with the optional `ref` input (SHA, tag or
  branch). The workflow resolves it to a full SHA and deploys that commit.
- There is a single environment (Render free tier), so there is no separate dev stage.

Setup:

1. In Render: *New > Blueprint*, pick this repository (it reads `render.yaml`). Then, in the service
   settings, copy the **Deploy Hook** URL (it includes `?key=...`).
2. In GitHub (*Settings > Environments*): create the `production` environment.
3. Add the secret `RENDER_DEPLOY_HOOK` (the hook URL) and the variable `RENDER_URL` (the public
   service URL, without a trailing slash).

Limitations: the free service sleeps after 15 minutes without traffic and takes about a minute to
wake; the disk is ephemeral (the database is rebuilt from the seed on every start); there is no live
inference, only the replayed recording; the YouTube live stream is not available there.

## Observability

Optional and off by default. The API (`cvp.api.telemetria`) emits OpenTelemetry only when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set; without it nothing is imported or started, so the light
Render image and the test suite behave as before. The key user journey is the dashboard asking
`GET /api/v1/projects/{id}/metrics/latest` (and `/metrics`), which reads the database.

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://observability:4318 PROJECT=projects/00-demo   docker compose --profile observability up -d --build       # bash
# PowerShell: $env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://observability:4318"; ...
```

(or put `COMPOSE_PROFILES=observability` and the endpoint in `.env`). Grafana is at
<http://localhost:3000> (admin/admin, anonymous viewing as Admin: local use only); OTLP listens on
4317 (gRPC) and 4318 (HTTP). The `observability` service is `grafana/otel-lgtm` (Grafana, Tempo,
Loki, Prometheus in one container); `ops/grafana/` is mounted into it and provisions everything.

| Signal | What is emitted | Where to look |
|---|---|---|
| Traces | one per request (FastAPI) with a child span per SQL query (SQLAlchemy) | Explore > Tempo, or the "Recent traces" table of the dashboard |
| Metrics | request count and latency per route (`http_server_request_duration_seconds_*`) and the custom gauge `cvp_data_freshness_seconds{project}`: seconds since the newest `metric_minute` row of each project, computed at export time | dashboard "CV Platform – user journey" (Dashboards > CV Platform) |
| Logs | one JSON line per request on stdout and over OTLP, with `trace_id` and `span_id` of the request span | dashboard log panel, Explore > Loki: `{service_name="cvp-api"} \| trace_id="<id>"` |

The `trace_id` in the JSON log line is the id of the trace (`docker compose logs api` shows it;
Loki stores it as structured metadata, and the image's own datasources link Loki and Tempo by
`trace_id`). No
customer data: there are no users, and still no headers, cookies, query strings, client IP or
user agent are exported (spans are scrubbed, logs carry method, route template, status and
duration only). The worker is not instrumented. Screenshots: [`docs/observability/`](docs/observability/)
(`dashboard.png`, `alert-rules.png`, `trace.png`).

### Alerting on user impact

`ops/grafana/provisioning/alerting/cvp.yaml` (a provisioned file, no clicking):

- **`DataStale`**: `cvp_data_freshness_seconds > 180` for 2 minutes, i.e. the user opens the
  dashboard and sees data older than 3 minutes (the worker stopped). Labels:
  `alertname=DataStale`, `service=worker`, `project=<id>`. Only projects with data from the last
  6 hours are considered, so an old project nobody runs does not page anyone. Note that for file
  sources (`00-demo`) the data timestamps follow the video, not the wall clock, so on a machine
  slower or faster than real time the freshness drifts; it is meaningful for live sources.
- **`ApiErrorRate`** (`service=api`): more than 5 % of API requests are 5xx for 5 minutes.
- Contact point `oncall-agent`: webhook `POST http://host.docker.internal:9000/alert` (where
  `ops/responder/` listens), standard Grafana webhook JSON, grouped by `alertname` and `project`.
  A real firing and resolved payload is in
  [`docs/observability/alert-webhook-example.json`](docs/observability/alert-webhook-example.json).
  If nobody listens, Grafana logs the refused connection and retries every minute.

Try it: `docker compose stop worker`; after about 3 minutes the rule is Pending, 2 minutes later
Firing; `docker compose start worker` resolves it. After editing the dashboard JSON, run
`docker compose --profile observability restart observability` (the bind-mounted file is not
re-read on Docker Desktop for Windows).

## Status

Under construction. The backlog is in the GitHub issues; the initial history is in
[`_docs/tasks.md`](_docs/tasks.md).

## On-call agent

An alert (`DataStale`, the worker stopped producing fresh metrics) wakes `ops/responder/`: code
collects bounded evidence, a read-only `claude -p` call proposes `restart_worker` or `none`, and
code only executes what is in its allowlist, with a rate limit, then verifies recovery and records
the incident. See [`ops/responder/README.md`](ops/responder/README.md); a real incident is in
[`ops/incidents/example/`](ops/incidents/example/).
