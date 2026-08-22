# <PROJECT NAME>

<One or two sentences: what this project is and who it is for.>

## Commands

<!-- TODO: replace with the real commands for your stack. Run each one yourself
     before writing it down here. A command that does not work is worse than no
     command: the agent will keep retrying it. -->

- `<uv sync>` - install dependencies
- `<uv run pytest>` - run the whole test suite
- `<uv run pytest tests/test_home.py>` - run one test file
- `<uv run python manage.py runserver>` - run the app locally

## Rules

- Dependencies are added in `<pyproject.toml>`. Do not add one without asking.
- Commit regularly.
- Implement against the acceptance criteria in the issue. Do not change them.
- Prefer small, reviewable diffs.
- Do not modify CI/CD, secrets, or deployment configuration without asking.
- Do not close GitHub issues. Only the orchestrator closes issues.

## Documents

- `_docs/plan.md` - what we are building and for whom
- `_docs/process.md` - how work is organized, roles and lifecycle
- Before writing tests, read `_docs/testing-guidelines.md`
- For anything touching the UI, read `_docs/design-system.md`
- For anything touching the API, read `_docs/api.md`

## Backlog

Tasks are GitHub issues. The issue is the source of truth for what to build.

<!-- Keep this file SHORT. It is loaded in every single session.
     Everything else belongs in _docs/ and gets linked from here, so it is
     loaded only when it is relevant to the task at hand. -->
