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
- `execute_purge(policy_name: str, confirmation_token: str, params: dict[str, int | float | str] | None = None)` (amended, see Rulings)
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
3. A policy with no params previews and executes with `params` omitted, `None`, or `{}`.
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
14. NaN and Infinity are rejected for a number param without touching the database.
15. A test through the real tool layer shows JSON true is rejected for a number param.
16. A policy whose select joins a to-many relation reports a count equal to the number
    of distinct rows.
17. An unknown policy name containing a newline does not appear raw in any error or
    log output.
18. No preview response contains a model field name as a key.
19. A policy whose select names a nonexistent field raises `ImproperlyConfigured` at
    load.

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

## Rulings
Owner decisions made after the implementation plan was reviewed. Where a ruling
conflicts with text above, the ruling wins.

### Conflicts
- Signature: `execute_purge(policy_name: str, confirmation_token: str, params: ... | None = None)`.
  `params=None` is normalized to `{}` everywhere, including the hash.
- Coercion: validation must see values as the client sent them. JSON true must be
  rejected for a number param, not treated as 1. If the declared signature coerces,
  change the signature so it does not, and report the change.
- Preview `sample_rows` key each value by param name, never by field name.
- `_row_set_snapshot` ordering by pk is accepted. Over-cap executes are refused anyway,
  so the slice only affects the stored fingerprint.
- Stale docs: add a one-line "Amended by ADR 4" note under ADR 1, 2, and 3 where they
  describe the old hash, sample, or ordering. Bodies are not rewritten. The CLAUDE.md
  Tool surface section and the `validate_policy` mention are updated to match the code.
- Moving the shared guards into `dbpurge/policies.py` is approved.

### Ambiguities
1. Tools call `load_policies()` on every call, with no module-level cache. `handle()`
   also loads once so bad config stops startup. If load fails during a tool call, the
   client gets an opaque error and the detail is logged server-side.
2. Windows: equal bounds are valid only when both ops are inclusive. Any strict op with
   equal bounds is empty and rejected.
3. Two `choice` params on the same field are rejected at load, with a rule and a test.
4. `choices` is a non-empty list of str or of int, one type only, no duplicates, no
   bool, None, or float. str choices need a character field, int choices an integer
   field. Request values are matched by exact type and value.
5. `number`: min and max are finite int or float, never bool. Numeric field means the
   integer field family, FloatField, or DecimalField. For DecimalField, compare using
   `Decimal(str(value))`. No param of any type may target the primary key or a
   relational field.
6. `load_policies()` compiles each policy's query without executing it, so a Q naming a
   missing field raises `ImproperlyConfigured` at load. If compiling needs a live
   database connection, stop and report.
7. Resolved by ruling 1.
8. The validated values are hashed as received. 5 and 5.0 give different tokens. That
   fails safe and is recorded in ADR 4.
9. `list_policies` exposes `op` for `age_seconds` and `number` params. The return
   structure is typed as precisely as strict mypy allows.
10. For a DateField param, compare against the cutoff's date, not an aware datetime,
    with a test. The cron path is untouched.

### Batch A review rulings
- Integer fields: a `number` param on a field in the IntegerField family must have int
  `min` and `max` at load (a float bound is refused), and the request value must be an
  int. A float such as -5.5 can never reach the query, where the field would truncate
  it silently.
- Empty Q: `select` and `exclude` must not be an empty Q. A Q with no children is
  refused at load, including a negated empty Q. A policy needs a non-empty `select` or
  at least one param.

### Batch B rulings
- Over-cap previews are refused: a preview whose match count exceeds the effective cap
  raises an error and issues no token. The error reports the match count and the
  effective cap and never echoes agent-supplied text. This is a behavior change from
  1.x, where preview reported any count. Record it in the release-2-0 migration note.
- A policy load failure at startup stops the command with `CommandError`, carrying the
  detail and chained from the `ImproperlyConfigured` that `load_policies()` raised.
- `DB_PURGE_MCP_MAX_ROWS` must be a positive integer, validated whenever policies load,
  so a value of 0 is invalid. In 1.x a value of 0 blocked every purge. This is a
  behavior change. Record it in the release-2-0 migration note.
- Internal invariants are checked with explicit raises, never `assert`, since asserts
  are stripped under `-O`.

### Batch B review rulings
- A token presented with a different policy or different params is popped and not
  reinstated, so that attempt burns it and the correct execute afterwards is rejected.
  This is kept deliberately. Only failures after the token matches (row cap, drift,
  database errors) reinstate it, and a request rejected before the token check (an
  unknown policy or invalid params) leaves it alone. The execute docstrings say so.

### Additional requirements
- Non-finite numbers (NaN, Infinity, -Infinity) are rejected in requests and in config
  bounds. For `age_seconds`, max must be small enough that now minus max is a
  representable datetime.
- Policy names must match `^[a-z][a-z0-9_]{0,63}$`.
- Agent-supplied strings are never echoed raw into error messages or log lines.
- The matched queryset returns each row exactly once even when select or exclude joins
  across a to-many relation, and stays compatible with `select_for_update`.

### ADR 4 accepted limitations to record
- Aggregate cap bypass by slicing params across calls.
- Preview as a read oracle on param fields.
- Tokens are not bound to the policy definition.
- Settings-time values are frozen at startup.
- Token store growth from repeated previews.

### Review gates
Batch A is commits 1 to 4, batch B is commits 5 and 6, batch C is commits 7 to 9.
Each batch is shown as a full diff and approved before it is committed. Before commit 6,
a venv with `pip install ".[mcp]"` is used to run `tests/e2e_stdio.py` and a real
tool-layer coercion test.
