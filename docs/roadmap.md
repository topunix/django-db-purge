# django-db-purge roadmap

## How to read this file
This file records decisions, not release state, and it is not guaranteed current.
Before acting on any item, check its "Verify" marker against `master`. If the marker is
present, the item has shipped and this file is stale; say so and skip it.

State sources:
- `master` itself, for whether a setting, module, or file exists
- PyPI: https://pypi.org/pypi/django-db-purge/json (`info.version`)
- GitHub releases and tags: `topunix/django-db-purge`

Items reference each other by name, never by number. Only the maintainer reorders or
adds items.
Task specs live in `docs/tasks/`, versioned, deleted after the work lands on `master`.
Shipped items are deleted in the PR that ships them.

## Sequenced queue

Sequencing: Lint configuration comes first, so later branches are linted from the
start. The policy surface comes next, then logging, then the harness, then the
release. The harness attacks the final 2.0 interface once instead of being rewritten
after a signature change, and logging lands before it so attack cases can assert on what
the server recorded. Attack registry design happens in chat while the policy items are
implemented.

1. **lint-config**: add ruff and mypy configuration and run both in CI. Neither tool
   has any configuration in the repo today.
   Decided: configuration lives in `pyproject.toml` under tool sections only.
   Packaging stays in `setup.py`.
   Resolve before starting (owner decisions):
   - Ruff rule set.
   - Mypy strictness.
   Verify: the CI workflow on `master` runs ruff and mypy.

2. **policy-registry**: replace the date-only MCP interface with operator-defined named
   policies. The agent selects a policy by name and supplies only bounded params. It
   never authors a filter.
   Decided: setting name is `DB_PURGE_MCP_POLICIES`. Policy keys are `select`,
   `exclude`, `field`, `param` (with `min` and `max`), and `action`, which defaults to
   `"delete"` so later verbs are additive.
   Decided: `list_policies` is the discovery tool and returns each policy's name,
   description, tunable param, and bounds. `preview` and `execute` take `policy_name`
   plus params.
   Decided: this is a breaking change to the `days` signature and ships as 2.0.0.
   Rejected: `DB_PURGE_POLICIES` as the name. Too close to the existing cron setting
   `DB_PURGE_RETENTION_POLICIES`, and it drops the MCP settings prefix.
   Guardrails: the token must bind policy name, params, and row set. Row cap,
   single-use tokens, TTL, and row-set fingerprinting are unchanged.
   Resolve before starting (owner decisions):
   - One tunable param per policy, or more than one (a windowed date range needs two).
   - Whether the old `days` call is removed outright or kept as a deprecated shim.
   - What happens to the flat model allowlist setting once policies name their models.
   Verify: `DB_PURGE_MCP_POLICIES` is read by the MCP server on `master` and a
   `list_policies` tool exists.

3. **retention-holds**: a hold predicate that every policy respects.
   Decided: the hold is ANDed into every policy unconditionally, at both preview and
   execute, and a policy definition cannot override it.
   Decided: if a hold lands between preview and execute, the row set shrinks and the
   result is reported as a narrowing, not rejected as drift. Growth stays impossible.
   Depends on: policy-registry.
   Resolve before starting (owner decisions):
   - Field convention: `legal_hold` boolean, `hold_until` timestamp, or both, and
     whether the field name is configurable.
   - Behavior for a model with no hold field.
   Verify: a test on `master` asserts a held row survives an execute whose preview
   matched it.

4. **cascade-preview**: preview responses report per-related-model cascade counts, so
   the agent and operator see what a delete will take with it.
   Depends on: policy-registry.
   Resolve before starting (owner decision):
   - Whether cascaded rows count toward `DB_PURGE_MCP_MAX_ROWS`. Relates to the
     suspected aggregate row cap gap in adversarial-harness.
   Verify: a preview response on `master` includes cascade counts keyed by related
   model.

5. **structured-logging**: one stdlib `logging` record per tool call, emitted where a
   token is issued, consumed, or rejected.
   Decided: fields are policy name, params, row count, outcome, `client_name`, and
   `client_version`. MCP stdio gives client identity from the `initialize` handshake,
   not model identity, and DESIGN.md says so.
   Rejected: a field named for the agent model. It would hold a client string and
   overclaim.
   Rejected for now: a DB-backed audit model. See audit-model.
   Depends on: policy-registry, so records carry the policy name from the start.
   Verify: a test on `master` asserts a rejected execute emits a log record with an
   outcome field.

6. **adversarial-harness**: a declarative attack registry and runner that red-teams the
   MCP surface, with a published results table.
   Decided: categories are token lifecycle, parameter binding, row-set fingerprinting,
   allowlist and scope escape, row cap enforcement, and protocol/injection. The
   registry is revised in chat against the policy surface before implementation.
   Decided: tests are hermetic and deterministic, live in `tests/adversarial/` under
   their own pytest marker, and emit JUnit and JSON. The markdown results table is
   generated, not hand-written. A CI job runs the suite.
   Decided: a case the server fails to block is recorded as a finding with severity and
   reported with a proposed fix. It is never quietly patched.
   Decided: injection testing is a static payload corpus. A live agent loop is out of
   scope for CI and would be a separate opt-in script.
   Suspected gap: aggregate row cap bypass across multiple calls.
   Depends on: policy-registry, retention-holds, cascade-preview, structured-logging.
   Verify: `evals/README.md` on `master` contains a threat model and a results table.

7. **release-2-0**: version bump to 2.0.0, migration note showing the old `days` call
   expressed as a policy, README structural reorganization, PyPI publish through the
   existing `publish.yml` release workflow.
   Depends on: every item above.
   Also: fix the stale classifiers in `setup.py` (Python 3.5 and 3.6, Django 2.0) to
   match `python_requires`.
   Verify: PyPI `info.version` is 2.0.0 and the README has a migration section.

8. **otel-spans**: OpenTelemetry spans on tool calls, as an optional extra.
   Verify: an `[otel]` extra exists in `setup.py` on `master`.

## Unscheduled
- **mcp-elicitation**: human confirmation through the protocol above a configurable
  threshold.
- **anonymize-action**: a second `action` verb. Fits the existing preview, token, and
  fingerprint model nearly unchanged.
- **audit-model**: DB-backed append-only audit trail. Needs an explicit threat model for
  "immutable", and the model must be excluded from the purge tool's own reach.

## Rejected or deferred
- **archive action**: needs a destination, credentials, and partial-failure handling.
  At most a pre-delete export hook.
- **subject erasure**: much larger surface (FK cascade planning, proof of erasure,
  backup replay). Explicitly deferred.
- **SQLAlchemy support**: scope expansion with low return.
