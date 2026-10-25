# Final project checklist

The 12 peer-review criteria of **AI Dev Tools Zoomcamp 2026**, in tickable form, with the real
current status of each one in this repository.

- Maximum **24 points**; **10 points** are needed to pass.
- Each participant must also peer-review **3 other projects**.
- Status key: `[x]` done and verifiable in the repo, `[ ]` pending or not yet verified.

## Summary

| # | Criterion | Max | Status |
|---|---|---|---|
| 1 | Problem Description | 2 | Pending final review |
| 2 | Product Specification | 2 | Pending final review |
| 3 | Agent Team Specification | 2 | Pending final review |
| 4 | Context Management | 2 | Pending final review |
| 5 | Application | 2 | Done |
| 6 | Testing | 2 | Done |
| 7 | Containerization | 2 | Done |
| 8 | Deployment | 2 | Done: https://cv-platform-mrg8.onrender.com |
| 9 | CI/CD | 3 | Level 2 of 3 done |
| 10 | Observability | 2 | Done (local, `--profile observability`) |
| 11 | Alerting on user impact | 1 | Done (local, `--profile observability`) |
| 12 | On-call agent | 2 | Done (local, full loop run twice) |

## Criteria

### 1. Problem Description (0-2)

- [ ] The README describes the problem, the functionality and the expected behavior.

Status: pending final review. Material exists in [`README.md`](../README.md) ("The problem",
"What it does") and [`plan.md`](plan.md); not yet marked done.

### 2. Product Specification (0-2)

- [ ] A product spec exists.
- [ ] A public task tracker with acceptance criteria, with evidence of use by agents.

Status: pending final review. Spec: [`plan.md`](plan.md). Tracker: the GitHub issues of this
repository, each with acceptance criteria. Evidence of agent use still has to be gathered
(see [`ai-usage-report.md`](ai-usage-report.md) and [`process.md`](process.md)).

### 3. Agent Team Specification (0-2)

- [ ] The agent team (roles, lifecycle, process) is specified.

Status: pending final review. Roles and lifecycle: [`process.md`](process.md); role
definitions in [`team/`](team/) (`pm.md`, `software-engineer.md`, `qa-engineer.md`).

### 4. Context Management (0-2)

- [ ] `AGENTS.md` plus modular documents loaded on demand.

Status: pending final review. [`AGENTS.md`](../AGENTS.md) is short and links to modular
documents in `_docs/` (plan, architecture, process, testing guidelines, design system, API).

### 5. Application (0-2)

- [x] Full-stack application, working end to end, runnable locally with one command.

Status: done. Worker + API + React dashboard; `docker compose up --build` starts the whole
system (see [`README.md`](../README.md), "Quick start").

### 6. Testing (0-2)

- [x] Unit and integration tests, runnable with one command, and they pass.

Status: done. `uv run pytest` runs the backend suite (unit, component and integration
directories under `tests/`); the frontend has Vitest tests (`npm test` in `frontend/`).

### 7. Containerization (0-2)

- [x] The complete system, including the database, starts via Docker Compose with one command.

Status: done. `compose.yaml` defines `db` (Postgres 17), `api`, `worker` and `web`; start it
with `docker compose up --build`.

### 8. Deployment (0-2)

- [x] A public URL, or a clear demo of the deployed system: <https://cv-platform-mrg8.onrender.com>.

Status: done, deployed on 2026-10-25 (deploy workflow run green, including the smoke test). Render free tier (Docker), a single environment.
It serves a light image (`docker/render.Dockerfile`, blueprint `render.yaml`) with the real
dashboard and API over a recorded 30-minute run replayed in a loop (not live inference); the
full image with the detector is `docker compose up`. The free service sleeps after 15 minutes
without traffic, so the first request can take about a minute.

### 9. CI/CD (0-3)

- [x] Level 1: tests run on every pull request.
- [x] Level 2: level 1 plus automatic continuous delivery.
- [ ] Level 3: level 2 plus deployment to a dev environment and a manual deployment to production.

Status: level 2, verified with a real deploy. `.github/workflows/ci.yml` runs on every push to
`main` and on every pull request: ruff lint, ruff format check,
`uv run pytest -m "not slow and not network"`, and for the front end `npm test` and
`npm run build`. `.github/workflows/deploy.yml` runs when CI succeeds on `main` (or by hand, with
a `ref` to roll back): it calls the Render deploy hook and smoke-tests `/api/v1/projects`. It
needs the `RENDER_DEPLOY_HOOK` secret and the `RENDER_URL` variable. Level 3 is not planned: the
free tier gives a single environment.

### 10. Observability (0-2)

- [x] Structured logs, metrics and traces sharing the same trace ID, with no customer data,
      visible in a UI.

Status: done, verified by running it. With `docker compose --profile observability up` the API
exports OpenTelemetry (`backend/src/cvp/api/telemetria.py`) to a `grafana/otel-lgtm` container:
one trace per request with SQL spans (Tempo), request count/latency per route and the
`cvp_data_freshness_seconds{project}` gauge (Prometheus), and one JSON log line per request
(stdout and Loki). For one `GET /api/v1/projects/{id}/metrics/latest` the same `trace_id` was
found in the stdout JSON log, in Tempo and in Loki. Spans are scrubbed of client IP, user agent
and query string; no headers or cookies are captured. Provisioned dashboard
"CV Platform – user journey" (p95 latency, 5xx rate, traffic, data freshness, traces and logs of
the key route): [`dashboard.png`](../docs/observability/dashboard.png),
[`trace.png`](../docs/observability/trace.png). How to run it: README, section "Observability".
It is off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so the Render deployment does not have it.

### 11. Alerting on user impact (0-1)

- [x] Alerts fire on user-visible impact.

Status: done, verified by running it. `ops/grafana/provisioning/alerting/cvp.yaml` provisions
`DataStale` (`cvp_data_freshness_seconds > 180` for 2 min: the user sees data older than 3
minutes; labels `alertname=DataStale`, `service=worker`, `project=<id>`), `ApiErrorRate` (5xx
above 5 %, not fired in the test) and a webhook contact point to
`http://host.docker.internal:9000/alert`. Test: `docker compose stop worker` took the rule
Normal -> Pending (3.3 min after the stop) -> Firing (2 min later) and Grafana POSTed the
webhook (first refused, nobody listening; then delivered to a throw-away listener); after
`docker compose start worker` the alert resolved and a `resolved` webhook arrived. Real payloads:
[`alert-webhook-example.json`](../docs/observability/alert-webhook-example.json); rule list:
[`alert-rules.png`](../docs/observability/alert-rules.png). The on-call agent (criterion 12)
consumes this webhook; the full loop was run with the real Grafana webhook (see criterion 12).

### 12. On-call agent (0-2)

- [x] An agent that handles on-call duties (reacts to alerts).
- [x] Verified end to end with the real components (no simulated alert), twice.

Status: done, verified by running it on 2026-10-25. `ops/responder/responder.py` listens on the
host (`127.0.0.1:9000`, reachable from Grafana as `host.docker.internal:9000`, no token or
`0.0.0.0` needed on Docker Desktop for Windows). Test: temporary project `99-e2e-test` (copy of
`00-demo`, `imgsz: 320`, `target_fps: 17` so that the data timestamps follow the wall clock),
`docker compose stop worker`, then nothing else. Run 2 (the one in the screenshots), UTC:

| Time | Event |
|---|---|
| 21:15:36 | `docker compose stop worker` |
| 21:20:35 | Grafana `DataStale` Firing, real webhook received (`alert_received`), 5.0 min after the stop |
| 21:20:48.9 | real `claude -p` (haiku, no tools) decides `restart_worker` (13 s after the alert) |
| 21:20:49.5 | `docker compose restart worker` run by code from the allowlist |
| 21:20:59.6 | verification: newest sample 60 s old, outcome `recovered` (24 s after the alert) |
| 21:21:20 | rule instance back to Normal; 21:21:35 Grafana sent `resolved`, accepted by the responder (200, ignored; Grafana reports no notify error) |

Run 1 gave the same result (alert 21:13:35, restart 21:13:46, recovered 21:13:56, resolved
21:14:35). Evidence: incident JSON and bounded evidence in
[`ops/incidents/example/`](../ops/incidents/example/) (`20261025T212035Z-*.json`,
`20261025T211335Z-incident.json`); Grafana, rule instances Firing and then Normal:
[`alert-firing-e2e.png`](../docs/observability/alert-firing-e2e.png),
[`alert-normal-e2e.png`](../docs/observability/alert-normal-e2e.png). A side result of the same
session: `20261025T210235Z-incident.json` is a real `no_action`: Grafana also fired `DataStale`
for the stale rows of `00-demo` (nobody runs it) while another worker was healthy, and the agent
correctly declined to restart. Not verified: the `not_recovered` and rate-limit `escalated`
paths against the real stack (covered only by unit tests). Reproduce it: README, "How to
reproduce the full on-call demo".

---

**Points confirmed so far: 15 / 24** (Application 2, Testing 2, Containerization 2, Deployment 2, CI/CD 2, Observability 2, Alerting 1, On-call agent 2). Criteria 1-4 (8 points) stay pending final review.
Pass mark: 10.
