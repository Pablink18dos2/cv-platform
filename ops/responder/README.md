# On-call responder

A small agent that wakes up when the `DataStale` alert fires, investigates, and either fixes the
problem (restarting the worker) or escalates to a human. It covers criterion 12 (On-call agent).

## Architecture

```
Grafana alert rule (DataStale, service=worker, project=<id>)
   |  webhook POST /alert        (Grafana in Docker -> host.docker.internal:9000)
   v
responder.py  (host process, stdlib only)         answers 200 at once, works in a thread
   1. validate: only status=firing + alertname=DataStale + service=worker, project id sanitized
   2. evidence (built by CODE, bounded):  docker compose ps (5 fields), last 80 worker log
      lines (<= 8 KB, secret-looking lines dropped), age of the last metric sample from
      GET /api/v1/projects/<project>/metrics/latest        -> ops/incidents/<ts>-evidence.json
   3. agent: `claude -p --model haiku --tools ""` reads the evidence and replies with JSON
      {"diagnosis", "action": "restart_worker" | "none", "reason"}
   4. code validates the JSON strictly; anything odd becomes action=none
   5. code executes the action only if it is in the allowlist and the rate limit allows
   6. verification: up to 3 min waiting for the last sample to be younger than 120 s
   7. incident record -> ops/incidents/<ts>-incident.json (and structured lines on stdout)
```

The incident timeline runs from the first alert to recovery: `alert_received`,
`evidence_collected`, `agent_decision`, `action_executed` (or `action_skipped`),
`verification`, and `escalated` when it did not work. `outcome` is one of `recovered`,
`not_recovered`, `escalated` (rate limit hit) or `no_action`. A real example is in
[`../incidents/example/`](../incidents/example/).

## Guardrails

- **The model is read-only.** It is launched with `--tools ""` (no tools at all), an empty
  working directory, no settings/hooks (`--setting-sources ""`) and no session persistence. It
  can only emit text. The evidence is passed as data, and the prompt tells it to ignore any
  instruction found inside it.
- **The allowlist lives in code** (`ALLOWED_ACTIONS` in `responder.py`). The only action is
  `docker compose -f <repo>/compose.yaml restart worker`: fixed path, fixed service, no shell,
  and no part of the command comes from the model; it only picks a key. An unknown action is
  rejected and becomes `none`.
- **Strict validation.** The reply must be one JSON object with exactly `diagnosis`, `action`
  and `reason`, all strings (one code fence is tolerated). Any other shape, a failed or timed
  out agent call, or a non-zero exit means `action=none`.
- **Rate limit.** At most one restart every 10 minutes. A second restart request inside the
  window is not executed: the incident is recorded as `escalated`. If the alert persists after
  a restart (3 min without recovery) the outcome is `not_recovered` with an `escalated` entry:
  the responder does nothing more and a human takes over.
- **No secrets in the evidence.** Log lines containing `token`, `key`, `password`, `secret`,
  `hook`, `database_url`, `dsn` or `user:pass@` URLs are dropped before anything is stored or
  sent to the model; `docker compose ps` is reduced to `Service/State/Status/Health/ExitCode`
  (no env, labels or mounts). Evidence size is capped.
- **One incident at a time.** Alerts that arrive while one is being handled are acknowledged
  and ignored (Grafana re-notifies on its repeat interval).
- Optional: set `RESPONDER_TOKEN` and configure the Grafana contact point with
  `Authorization: Bearer <token>`; requests without it get 401.

## Permissions

| Actor | Can | Cannot |
|---|---|---|
| The model (`claude -p`) | read the prompt, write text | run tools, read files, run commands, reach the network |
| `responder.py` | `docker compose ps/logs worker`, GET the API, `docker compose restart worker`, write `ops/incidents/` | any other docker/compose command, any shell, any model-chosen argument |

The process runs as the local user, so it can in principle do whatever that user can do; the
restriction is the code path, which is small and covered by tests. Do not run it on a shared
host without a dedicated user.

## Running it

```
uv run python ops/responder/responder.py [--host 127.0.0.1] [--port 9000] [--api-url http://localhost:8080]
```

Requires `docker` and `claude` (Claude Code CLI, already authenticated) on the `PATH`.
`--api-url` (or `RESPONDER_API_URL`) is where `metrics/latest` is read; use
`http://localhost:8000` when the web container is not running. Grafana must post to
`http://host.docker.internal:9000/alert`. If Grafana cannot reach a loopback-only listener on
your setup, start the responder with `--host 0.0.0.0` (and set `RESPONDER_TOKEN`).

## Testing it

Unit tests (fake `claude` and `docker`, no network): `uv run pytest tests/unit/test_responder.py`.

End to end, simulating the alert instead of waiting for Grafana:

```
PROJECT=projects/00-demo docker compose up -d worker
uv run python ops/responder/responder.py --api-url http://localhost:8000 &
docker compose stop worker        # then wait > 3 min for the last sample to age (or skip)
curl -X POST localhost:9000/alert -H 'Content-Type: application/json' -d '{
  "status":"firing",
  "alerts":[{"status":"firing","labels":{"alertname":"DataStale","service":"worker","project":"00-demo"}}]}'
```

The worker is restarted and `ops/incidents/<ts>-incident.json` ends in `"outcome": "recovered"`.
`ops/incidents/` is git-ignored except `example/`.

## What it does not do

- It does not decide anything the allowlist does not contain: no rollbacks, no scaling, no
  changes to config, database or images, no restarts of `api`, `db` or `web`.
- It does not retry after a failed restart and does not page anyone: escalation is only a
  record in the incident file and on stdout (wire a notifier here if needed).
- Rate-limit state is in memory: restarting the responder forgets the last restart.
- It handles one alert type (`DataStale` / `worker`); everything else gets a 200 and is ignored.
- The model sees a snapshot, not a live system; it cannot ask for more evidence.
