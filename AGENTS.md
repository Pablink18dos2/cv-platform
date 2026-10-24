# CV Platform

A local computer-vision platform. A **project** connects a video source (connector) to a
detection model and one or more analyzers, and all projects share the same web front end.
All inference runs locally, with no GPU and no paid services.

## Commands

- `uv sync` - install dependencies
- `uv run pytest` - the whole suite (excludes tests marked `network`)
- `uv run pytest tests/unit/test_smoke.py` - one file
- `uv run pytest -m "not slow and not network"` - only the fast ones, which is what CI runs
- `uv run ruff check .` and `uv run ruff format .` - lint and format
- `uv run cvp --version` - the CLI

## Rules

- Dependencies are added in `pyproject.toml`. Do not add one without asking.
- Commit regularly.
- Implement against the acceptance criteria in the issue. Do not change them.
- Prefer small, reviewable diffs.
- Do not modify CI/CD, secrets, or deployment configuration without asking.
- Do not close GitHub issues. Only the orchestrator closes issues.

### Project-specific rules

- **Blocks communicate only through the contracts in `cvp.core`.** An analyzer does not know
  which source a frame came from or which model detected it. If adding something requires
  touching the pipeline, the design is broken: fix the design.
- **Nothing in an external library's format leaves its adapter.** ultralytics objects are
  converted to `Detection` inside `detectors/` and stay there.
- **No test in the default suite touches the network or downloads weights.** Anything that needs
  the internet is marked `@pytest.mark.network`; anything that loads a real model, `@pytest.mark.slow`.
- **No `datetime.now()` in testable logic.** Time is injected, so hysteresis and aggregations
  can be tested with a fake clock.
- **No license plates, faces or crops of people are stored.** This is an explicit non-goal of the plan.
- Model weights go to `models/` and are not committed.

## Documents

(Detail documents other than `plan.md` and `process.md` are written in Spanish.)

- `_docs/plan.md` - what we are building and for whom, and what is out of scope
- `_docs/architecture.md` - the blocks, their contracts and how to add a new one.
  **Read it before touching the pipeline, a connector, a detector or an analyzer.**
- `_docs/process.md` - how work is organized, roles and lifecycle
- Before writing tests, read `_docs/testing-guidelines.md`
- For anything touching the UI, read `_docs/design-system.md`
- For anything touching the API, read `_docs/api.md`

## Backlog

Tasks are GitHub issues. The issue is the source of truth for what to build.

<!-- Keep this file SHORT. It is loaded in every single session.
     Everything else belongs in _docs/ and gets linked from here, so it is
     loaded only when it is relevant to the task at hand. -->
