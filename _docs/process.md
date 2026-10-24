# Process

- Tasks are GitHub issues, one at a time
- Read the acceptance criteria before starting and before closing
- Commit regularly

## Roles

- **PM** - grooms a task before anyone implements it, follows `_docs/team/pm.md`
- **Engineer** - implements one groomed task, follows `_docs/team/software-engineer.md`
- **QA** - checks the result against the acceptance criteria, follows `_docs/team/qa-engineer.md`

Each role runs in its own session. The agent that wrote the code does not judge the code.

## Orchestrator

The main session is the orchestrator. It launches the PM, the engineer and QA as
subagents. It does not groom, implement or test itself.

### Lifecycle

1. Pick the next open issue from the backlog
2. PM grooms it
3. Engineer implements it
4. QA verifies it
5. On FAIL, back to step 3 with the QA comment as input
6. On PASS, close the issue
7. Repeat until the backlog is empty

### Rules

- Do not skip step 2
- The engineer does not close the issue
- QA does not fix the code, only outputs PASS or FAIL
- The orchestrator closes the issue only after QA outputs PASS

## Two levels of ceremony

Decided on 2026-08-23, once the cost of the first two cycles had been measured: issue #2 cost
386k subagent tokens and issue #3 cost 1.06M, because a FAIL forces a full engineer + QA round
to be repeated. The full cycle does find real problems, but it is expensive, and not every task
carries the same risk.

**Full cycle (PM -> Engineer -> QA, each in its own session)** for tasks where a mistake is
costly and hard to spot by eye:

- Business logic: the analyzers (#10 traffic, #11 parking)
- Contracts that others consume: the API and its `openapi.yaml` (#12)
- Any task whose result is a number that someone is going to believe

**Light cycle (the orchestrator grooms, implements and verifies against the criteria)** for
mechanical tasks, where a failure is visible when you run it: connectors, adapters, packaging
(#4, #7, #15) and small follow-up tasks.

### What the light cycle gives up

In the light cycle, whoever writes the code is the one who judges it, which is exactly what the
full cycle exists to prevent. This is accepted knowingly and only where failure is visible at
run time. In exchange, the light cycle requires:

- Walking through the acceptance criteria one by one and naming the command that proves each one.
- Pasting the real output of the commands, not describing it.
- If there is any doubt about whether something is right, escalate to the full cycle. The doubt
  is the signal.

### Criteria are fixed in the issue, never in the code

Both FAILs on issue #3 were defects in the issue text, not in the code: an error message that
named a field with a default value (and a field with a default can never be missing), and a
per-test time budget that ignored that a subprocess importing pydantic has a ~1 s floor. Both
were fixed by editing the issue and leaving a dated note explaining why. That is the cheap way
to fix it; fixing it in the code leaves the issue text lying forever.
