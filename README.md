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

The demo runs for free on [Hugging Face Spaces](https://huggingface.co/docs/hub/spaces-sdks-docker)
(Docker SDK, CPU basic: 2 vCPU / 16 GB). One image ([`docker/space.Dockerfile`](docker/space.Dockerfile))
runs nginx on port 7860 (dashboard + `/api` proxy), the API and one worker for
`projects/00-demo`, with SQLite in the container's disk.

- **Automatic**: [`deploy.yml`](.github/workflows/deploy.yml) deploys to the Space when the CI
  workflow succeeds on `main`, behind the `production` environment.
- **Manual and rollback**: *Run workflow* in the Actions tab. The optional `ref` input deploys
  an older commit or tag, which is the rollback.
- There is a single Space because a free Hugging Face account only allows one running Space at
  a time, so there is no separate dev stage.
- Each deploy uploads the repo to the Space, waits for it to be `RUNNING` and checks
  `/api/v1/projects`.

Setup: a Hugging Face account, a write-access token as the `HF_TOKEN` repository secret, the
repository variable `HF_SPACE_PROD` (format `owner/name`) and a `production` environment
(Settings > Environments).

Limitations: the disk is ephemeral (history is lost on every restart or deploy); the free
Space sleeps after inactivity and takes a minute or two to wake; it runs the demo project
(a looping video file), not the YouTube live stream, which needs the network and ffmpeg.

## Status

Under construction. The backlog is in the GitHub issues; the initial history is in
[`_docs/tasks.md`](_docs/tasks.md).
