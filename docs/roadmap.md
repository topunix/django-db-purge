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

Sequencing: Postgres CI first, so the select_for_update path is tested before anything
builds on it. Logging lands before the harness so attack cases can assert on what the
server recorded. The harness attacks the 2.0 interface. Spans land before the release so
2.0 ships observable. Batched cron deletes follow the release and are measured against a
real load test before they are changed. retention-holds and cascade-preview are parked in
Unscheduled. If they return, they add their own sections to the attack registry.

1. **postgres-ci**: run the full test suite against PostgreSQL in CI, so the
   select_for_update branch in execute is exercised.
   Decided: a GitHub Actions Postgres service container, on one Python version. SQLite
   jobs stay.
   Resolve before starting (owner decision):
   - Postgres major version, matched to the RDS version the maintainer will deploy.
   Verify: .github/workflows/ci.yml on master has a job with a postgres service that runs
   runtests.py.

2. **structured-logging**: one stdlib `logging` record per tool call, emitted where a
   token is issued, consumed, or rejected.
   Decided: fields are policy name, params, row count, outcome, `client_name`, and
   `client_version`. MCP stdio gives client identity from the `initialize` handshake,
   not model identity, and DESIGN.md says so.
   Rejected: a field named for the agent model. It would hold a client string and
   overclaim.
   Rejected for now: a DB-backed audit model. See audit-model.
   Verify: a test on `master` asserts a rejected execute emits a log record with an
   outcome field.

3. **adversarial-harness**: a declarative attack registry and runner that red-teams the
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
   Depends on: structured-logging.
   Verify: `evals/README.md` on `master` contains a threat model and a results table.

4. **otel-spans**: OpenTelemetry spans on tool calls, as an optional extra.
   Verify: an `[otel]` extra exists in `setup.py` on `master`.

5. **release-2-0**: version bump to 2.0.0, migration note showing the old `days` call
   expressed as a policy, README structural reorganization, PyPI publish through the
   existing `publish.yml` release workflow.
   The migration note must also cover these behavior changes from 1.x:
   - A preview whose match count exceeds the effective row cap is refused and issues
     no token. In 1.x a preview reported any count and only execute enforced the cap.
   - `DB_PURGE_MCP_MAX_ROWS` must be a positive integer, so a value of 0 is now
     invalid. In 1.x a value of 0 blocked every purge.
   Depends on: postgres-ci, structured-logging, adversarial-harness, otel-spans.
   Also: fix the stale classifiers in `setup.py` (Python 3.5 and 3.6, Django 2.0) to
   match `python_requires`.
   Verify: PyPI `info.version` is 2.0.0 and the README has a migration section.

6. **cron-batched-delete**: the db_purge cron command deletes in bounded batches instead
   of one count() and one queryset.delete().
   Decided: measure first. Run it against a large, growing table under load and record
   the numbers before changing anything, so the change ships with a before and after.
   Decided: the MCP path is unchanged. It is already bounded by the row cap.
   Also: the completion log line passes the queryset itself as a format argument, which
   re-runs the query. Log the count instead.
   Resolve before starting (owner decisions):
   - Setting name and default batch size.
   - Whether a batch failure stops the run or continues with the next batch.
   Verify: a test on master asserts db_purge deletes in more than one batch when matches
   exceed the batch size.

## Unscheduled
- **django-stubs**: add Django type stubs and the mypy plugin so strict mode
  covers model and queryset types. Also removes the `disallow_subclassing_any`
  override in `pyproject.toml`.
- **mcp-elicitation**: human confirmation through the protocol above a configurable
  threshold.
- **anonymize-action**: a second `action` verb. Fits the existing preview, token, and
  fingerprint model nearly unchanged.
- **audit-model**: DB-backed append-only audit trail. Needs an explicit threat model for
  "immutable", and the model must be excluded from the purge tool's own reach.
- **retention-holds**: a hold predicate that every policy respects.
  Decided: the hold is ANDed into every policy unconditionally, at both preview and
  execute, and a policy definition cannot override it.
  Decided: if a hold lands between preview and execute, the row set shrinks and the
  result is reported as a narrowing, not rejected as drift. Growth stays impossible.
  Resolve before starting (owner decisions):
  - Field convention: `legal_hold` boolean, `hold_until` timestamp, or both, and
    whether the field name is configurable.
  - Behavior for a model with no hold field.
  Verify: a test on `master` asserts a held row survives an execute whose preview
  matched it.
- **cascade-preview**: preview responses report per-related-model cascade counts, so
  the agent and operator see what a delete will take with it.
  Resolve before starting (owner decision):
  - Whether cascaded rows count toward `DB_PURGE_MCP_MAX_ROWS`. Relates to the
    suspected aggregate row cap gap in adversarial-harness.
  Verify: a preview response on `master` includes cascade counts keyed by related
  model.
- **http-transport**: serve MCP over HTTP as well as stdio, so a deployed service can be
  reached over the network. Resolve before starting: the in-process token store limits
  this to one replica, so decide between a single replica and moving tokens to shared
  storage (Redis or a database table). See ADR 2.

## Rejected or deferred
- **archive action**: needs a destination, credentials, and partial-failure handling.
  At most a pre-delete export hook.
- **subject erasure**: much larger surface (FK cascade planning, proof of erasure,
  backup replay). Explicitly deferred.
- **SQLAlchemy support**: scope expansion with low return.
