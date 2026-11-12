# policy-registry

## Goal
Replace the date-only MCP interface with operator-defined named policies. The agent
selects a policy by name and supplies only declared, bounded params. It never names a
model, a column, or a filter.

## Scope

In:
- `DB_PURGE_MCP_POLICIES` setting, its schema, and fail-closed validation.
- `list_policies` tool. New signatures for `preview_purge` and `execute_purge`.
- Token parameter binding moved to policy name plus validated params.

Out:
- Retention holds and narrowing (retention-holds).
- Cascade counts and any `cascade_warn` key (cascade-preview).
- Logging changes beyond updating existing log lines (structured-logging).
- Version bump, migration note, README reorganization (release-2-0).
- The cron path: `db_purge` and `DB_PURGE_RETENTION_POLICIES` are untouched.
- Any `action` other than `"delete"`.

## Policy schema

```python
DB_PURGE_MCP_POLICIES = {
    "stale_failed_jobs": {
        "model": "jobs.Job",              # required, app_label.ModelName
        "description": "Failed or cancelled jobs past retention.",  # required
        "action": "delete",               # optional, default "delete"
        "select": Q(status__in=["failed", "cancelled"]),  # optional Q
        "exclude": Q(status="under_review"),              # optional Q
        "params": [                       # optional, 0 to 2 entries
            {"name": "older_than_seconds", "type": "age_seconds",
             "field": "created_at", "min": 2592000, "max": 315360000},
        ],
        "max_rows": 5000,                 # optional
    },
}
```

Param types:
- `age_seconds`: int value. `field` is a DateField or DateTimeField. `op` is `lte`
  (default, field <= now - value) or `gte`. Requires int `min` and `max`,
  0 < min <= max.
- `number`: int or float value, bool rejected. `field` is a numeric field. `op` is
  required, one of `lt`, `lte`, `gt`, `gte`. Requires `min` <= `max`.
- `choice`: value must equal one entry of `choices` (non-empty list), type-strict.
  Filters `field` by exact match.

Config rules, all enforced when policies load. Any violation raises
`ImproperlyConfigured` and the server does not start:
- Unknown top-level or param keys are rejected.
- `model` must resolve and must be in `DB_PURGE_MCP_ALLOWED_MODELS`.
- `action` must be `"delete"`.
- A policy must define `select` or at least one param.
- `select` and `exclude` must be `Q` objects.
- Param `field` must be a concrete field on the policy's model, with no `__`.
- Param names are unique within a policy.
- Two params on the same field must be the same type with opposite-direction ops.
- `max_rows` must be a positive int no greater than `DB_PURGE_MCP_MAX_ROWS`.

## Tools
- `list_policies()`: returns, per policy, `name`, `description`, `model`, `action`,
  effective `max_rows`, and each param's `name`, `type`, and bounds or choices. It
  never returns `select`, `exclude`, or param field names.
- `preview_purge(policy_name: str, params: dict[str, int | float | str] | None = None)`
- `execute_purge(policy_name: str, params: dict[str, int | float | str] | None, confirmation_token: str)`
- `list_purge_candidates` is removed.
- Return types are TypedDicts, as today. FastMCP derives the agent-facing schema from
  these signatures and docstrings, so they are part of the contract.

## Request validation
Runs before any database query, in both preview and execute, on the values as
received:
- Unknown policy name is rejected.
- Supplied param keys must equal the declared names exactly. Missing and extra keys
  are both rejected. There are no defaults.
- Each value must match its type and bounds, or be a declared choice. A bool is never
  accepted where a number is declared.
- A same-field pair must describe a non-empty window.
- The allowlist is re-checked on every call, as today.

## Guardrails that must not change
- Preview before execute, single-use token, 5 minute TTL, atomic pop, validate,
  reinstate.
- Row-set fingerprint and delete by stored PK list, with drift rejection.
- Row cap re-checked at execute. Effective cap is the policy `max_rows` if set, else
  `DB_PURGE_MCP_MAX_ROWS`.
- `DB_PURGE_MCP_FINGERPRINT_FIELDS` behavior.
- The optional fastmcp import fallback and the `handle` guard.

## Implementation notes
- Token params hash: SHA-256 over
  `json.dumps({"policy": name, "params": validated}, sort_keys=True, separators=(",", ":"))`.
  This replaces the `|` join in `_hash_params`.
- `_row_set_snapshot` loses its `time_column` argument and takes its deterministic
  slice in pk order.
- Preview `sample_rows` contain the pk plus the value of each param field. A policy
  with no params returns pk only.
- Policy loading and validation live in a new module, `dbpurge/policies.py`, with no
  fastmcp import, so it is usable without the `[mcp]` extra. Policies are validated on
  every load, and the management command loads them once before serving.
- New code passes strict mypy with no new overrides and no inline suppressions.

## Files touched
- `dbpurge/policies.py` (new)
- `dbpurge/management/commands/purge_mcp_server.py`
- `tests/test_purge_mcp_server.py`, `tests/test_policies.py` (new)
- `tests/models.py`: add a separate toy model for policy tests with a status field, a
  numeric field, a choice-like field, and a date field. Do not alter `SampleRecord`.
- `tests/settings.py`, `tests/demo_settings.py`, `tests/seed_demo.py`,
  `tests/e2e_stdio.py`
- `README.md`, `DESIGN.md`, `CLAUDE.md`, `docs/roadmap.md`

## Acceptance criteria
1. With `DB_PURGE_MCP_POLICIES` unset or empty, `list_policies` returns an empty list
   and every preview is rejected.
2. Each config rule above has a test proving the violation raises
   `ImproperlyConfigured`.
3. A policy with no params previews and executes with `params` omitted or `{}`.
4. A policy with one `age_seconds`, one `number`, and one `choice` param each preview
   and execute correctly, and each rejects an out-of-bounds or undeclared value
   without touching the database.
5. A two-param window policy deletes only rows inside the window and rejects an
   inverted or empty window at preview.
6. `exclude` is applied at both preview and execute. A row that becomes excluded
   between them causes drift rejection.
7. A token issued for one policy is rejected for another policy, and for the same
   policy with different params.
8. A policy `max_rows` below the global cap is enforced at preview and execute.
9. `list_policies` output contains no `select`, `exclude`, or field name.
10. No tool accepts `app_name`, `model_name`, `time_column`, or `retention_seconds`.
11. Every pre-existing guardrail test still passes after being ported to the policy
    signature. None is deleted without an equivalent replacement.
12. `tests/e2e_stdio.py` completes list, preview, execute, and a replay rejection over
    stdio.
13. `ruff check .`, `ruff format --check .`, `mypy`, and the full suite pass, locally
    and in CI on the pull request.

## Tests to add
- `tests/test_policies.py`: one test per config rule, one per param type and bound,
  window validation, canonical hash stability across param key order.
- `tests/test_purge_mcp_server.py`: criteria 1 and 3 through 10, plus ported
  token lifecycle, allowlist, row cap, and fingerprint tests.

## Docs to update
- `README.md`: Tools, Safety handshake, and Settings sections. Add
  `DB_PURGE_MCP_POLICIES` with one temporal and one non-temporal example. Also update
  the opening description and the Features list so neither describes the MCP path as
  date-only: purging is by operator-defined policy, and row age is one kind of policy.
  The cron section stays as is, since that path is still date-based. No structural
  reorganization and no migration note.
- `DESIGN.md`: add ADR 4 covering the registry, with Chosen, Rejected, and Accepted
  limitations. Record the decisions behind this spec and the rejected alternatives:
  agent-supplied filters, a `days` shim, dropping the allowlist.
- `CLAUDE.md`: update the safety rules that describe per-call validation of app,
  model, and column so they describe policy selection and bounded params.

## Roadmap item addressed
`policy-registry`. Delete it from `docs/roadmap.md` in this PR once
`DB_PURGE_MCP_POLICIES` is read by the server and `list_policies` exists.
