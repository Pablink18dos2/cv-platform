# API

<!-- Loaded only for API tasks. Linked from AGENTS.md.
     Relevant from Module 2 onwards, when there is a frontend and a backend. -->

## Source of truth

`openapi.yaml` is the contract between frontend and backend.

- If backend routes change, update `openapi.yaml` in the same commit
- If frontend API calls change, check them against `openapi.yaml` first
- Never invent an endpoint that is not in the contract. Add it to the
  contract first, then implement it.

> Coding agents hallucinate endpoints. The contract is what stops that from
> reaching production.

## Conventions

- Base path: `<\/api\/v1>`
- Auth: `<...>`
- Errors: consistent shape, e.g.

  ```json
  { "error": { "code": "duplicate_username", "message": "..." } }
  ```

- Status codes: 200 ok · 201 created · 400 validation · 401 unauthenticated ·
  403 unauthorized · 404 not found · 409 conflict

## Validation

- Validate request bodies against the schema, do not trust the client
- Return 400 with the field name that failed
