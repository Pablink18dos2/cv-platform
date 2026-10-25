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
| 10 | Observability | 2 | Pending |
| 11 | Alerting on user impact | 1 | Pending |
| 12 | On-call agent | 2 | Pending |

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

- [ ] Structured logs, metrics and traces sharing the same trace ID, with no customer data,
      visible in a UI.

Status: pending.

### 11. Alerting on user impact (0-1)

- [ ] Alerts fire on user-visible impact.

Status: pending.

### 12. On-call agent (0-2)

- [ ] An agent that handles on-call duties (reacts to alerts).

Status: pending.

---

**Points confirmed so far: 10 / 24** (Application 2, Testing 2, Containerization 2, Deployment 2, CI/CD 2).
Pass mark: 10.
