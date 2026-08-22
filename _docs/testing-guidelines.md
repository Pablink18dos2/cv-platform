# Testing guidelines

<!-- Loaded only for testing tasks. Linked from AGENTS.md. -->

## Commands

- Full suite: `<uv run pytest>`
- One file: `<uv run pytest tests/test_home.py>`
- Integration: `<docker compose up --build>`

## What to test

- Every acceptance criterion in the issue has at least one test
- The awkward cases, not only the happy path: empty input, duplicates,
  unauthorized access, missing data
- Behaviour, not implementation details

## What not to do

- Do not weaken or delete an existing test to make a new one pass.
  If a test is genuinely wrong, say so on the issue instead.
- Do not mock the thing you are trying to test
- Do not add a test that passes regardless of whether the code works

## Conventions

- Test files live in `<tests/>`
- Name tests after the behaviour: `test_duplicate_username_shows_error`
- Tests must not need network access or manual setup
