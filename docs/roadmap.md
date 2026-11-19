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
Task specs live in `docs/tasks/`, versioned.
Task specs are deleted in the PR that ships the work.
Shipped items are deleted in the PR that ships them.

## Sequenced queue

Sequencing: The harness attacks the 2.0 interface. Batched cron deletes follow the release
and are measured against a real load test before they are changed. retention-holds and
cascade-preview are parked in Unscheduled. If they return, they add their own sections to
the attack registry.

1. **release-2-0**: version bump to 2.0.0 and a migration note showing the old `days`
   call expressed as a policy. The maintainer publishes to PyPI through the existing
   `publish.yml` release workflow after the PR merges.
   Decided: the task never publishes, tags, or creates a GitHub release.
   The migration note must also cover these behavior changes from 1.x:
   - A preview whose match count exceeds the effective row cap is refused and issues
     no token. In 1.x a preview reported any count and only execute enforced the cap.
   - `DB_PURGE_MCP_MAX_ROWS` must be a positive integer, so a value of 0 is now
     invalid. In 1.x a value of 0 blocked every purge.
   Also: fix the stale classifiers in `setup.py` (Python 3.5 and 3.6, Django 2.0) to
   match `python_requires`.
   Verify: `setup.py` on `master` has version 2.0.0 and the README has a migration
   section.

2. **cron-batched-delete**: the db_purge cron command deletes in bounded batches instead
   of one count() and one queryset.delete().
   Decided: measure first. Run it against a large, growing table under load and record
   the numbers before changing anything, so the change ships with a before and after.
   Decided: the MCP path is unchanged. It is already bounded by the row cap.
   Also: the completion log line passes the queryset itself as a format argument, which
   re-runs the query. Log the count instead.
   Decided: the setting is DB_PURGE_BATCH_SIZE, a positive integer, default 1000. Any
   other value raises CommandError before any query.
   Decided: a batch failure stops the run with CommandError and a non-zero exit.
   Batches already deleted stay deleted. The error reports how many rows were deleted
   before the failure.
   Decided: the measurement is a script that seeds 500,000 expired rows and times the
   command before and after the change, on PostgreSQL. The numbers and the backend go
   in the PR description. The script is committed under benchmarks/.
   Verify: a test on master asserts db_purge deletes in more than one batch when matches
   exceed the batch size.

3. **roadmap-command-ship-cleanup**: make the /roadmap command delete the shipped
   roadmap entry and its task spec on the branch, inside the PR that ships the work.
   Decided: Both deletions happen in the shipping PR, not after merge. The command's
   report drops the "Roadmap entries to delete once merged" section and instead
   confirms both deletions are in the diff. The workflow text in CLAUDE.md is updated
   to match.
   Resolve before starting: Whether to add a CI guard that fails a PR when
   docs/tasks/<branch>.md still exists at the PR head, or when the roadmap entry named
   by the branch is still present.
   Verify: The /roadmap command definition instructs the implementer to delete the
   roadmap entry and docs/tasks/<slug>.md on the branch, and contains no "delete once
   merged" wording.

4. **audit-log-truncation**: cap the logged size of tool arguments so oversized inputs
   cannot bloat the audit log.
   Decided: Threshold is 4096 characters. Past it, log the first 4096 characters, the
   original length, and a sha256 of the full value.
   Resolve before starting: Whether truncation applies to accepted calls as well as
   rejected ones.
   Verify: injection-oversized-string reports blocked in evals/README.md.

5. **deletion-budget**: enforce a rolling-window deletion budget per policy so repeated
   preview and execute cycles cannot exceed the row cap in aggregate.
   Resolve before starting: Window length and default budget. Where budget state lives
   (database table or cache). Behavior across multiple server processes.
   Verify: cap-aggregate-bypass reports blocked in evals/README.md.

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
  - Whether cascaded rows count toward `DB_PURGE_MCP_MAX_ROWS`.
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
