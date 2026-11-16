# postgres-ci

## Goal

Run the full test suite against PostgreSQL in CI, so the select_for_update
branch in `_execute_loaded` (purge_mcp_server.py) is exercised against a real
database that supports row locking. Today every test runs on SQLite, where
`connection.features.has_select_for_update` is False, so that branch is only
covered by a mock.

## Scope

In:

- A new job in `.github/workflows/ci.yml` that runs `python runtests.py`
  against a GitHub Actions PostgreSQL service container. Service image
  `postgres:17`. One Python version (3.12). The package is installed with the
  `[mcp]` extra plus a PostgreSQL driver (`psycopg[binary]`, major version 3)
  installed in the job only, so the execute path and the tool tests run.
  The service has a health check so the tests start only once the server
  accepts connections.
- `tests/settings.py` selects the PostgreSQL backend when an environment
  variable asks for it (for example `DBPURGE_TEST_DB_ENGINE=postgresql`), with
  name, user, password, host, and port read from environment variables that
  have local defaults. With the variable unset, the settings are exactly as
  today (in-memory SQLite, `DBPURGE_TEST_DB` override still honored).
- Make the existing suite backend-neutral where it assumes SQLite. In
  particular `test_execute_succeeds_on_backend_without_select_for_update_support`
  asserts `has_select_for_update` is False and must instead be skipped on
  backends that support row locking (or simulate the unsupported case with a
  patch), with its intent unchanged.
- A new test, run only where `connection.features.has_select_for_update` is
  True, that executes a real preview and execute against the database with no
  mock of `select_for_update`, and asserts from captured SQL
  (`django.test.utils.CaptureQueriesContext`) that a `FOR UPDATE` query ran
  and the rows were deleted.
- A short README note under "Running the tests" on how to run the suite
  against a local PostgreSQL.

Out:

- Removing or changing the existing SQLite jobs. They stay as they are.
- Any change under `dbpurge/`. If PostgreSQL exposes a real bug, record it
  under Deviations and stop rather than redesigning the execute path.
- Adding a database driver to `setup.py` or any extra.
- More than one Python version or more than one PostgreSQL version for the
  new job.
- The db_purge cron command's batching (cron-batched-delete).
- docs/roadmap.md.

## Files touched

- .github/workflows/ci.yml
- tests/settings.py
- tests/test_purge_mcp_server.py (and any other test module only if it
  turns out to assume SQLite)
- README.md
- docs/tasks/postgres-ci.md (this file)

## Acceptance criteria

- `.github/workflows/ci.yml` has a job with a `services:` entry whose image is
  `postgres:17`, that job uses exactly one Python version, and its final step
  runs `python runtests.py`. Checked by reading the file.
- The existing `test`, `lint`, and `wheel-smoke-test` jobs are unchanged:
  `git diff origin/master -- .github/workflows/ci.yml` shows only additions.
- With no database environment variables set, `python runtests.py` passes on
  SQLite, and `tests/settings.py` still resolves to
  `django.db.backends.sqlite3` with `NAME` `:memory:`.
- A test exists that is skipped unless `connection.features.has_select_for_update`
  is True, does not patch `select_for_update` or `has_select_for_update`, and
  asserts a captured query contains `FOR UPDATE`. Checked by reading the file.
- On SQLite, no test asserts `has_select_for_update` is False unconditionally.
- If a local PostgreSQL can be started, `python runtests.py` passes against
  it with the environment variables set and the new test runs (not skipped).
  If one cannot be started, say so as unverified; CI is then the proof.
- `ruff check .`, `ruff format --check .`, and `mypy` pass.
- `git diff --stat origin/master` lists only the files under Files touched.

## Tests to add

- Real row-locking execute test on backends with `has_select_for_update`, as
  described in Scope.
- Adjust the SQLite-only assertion test so it is correct on both backends.

## Docs to update

- README.md, "Running the tests": how to point the suite at PostgreSQL with
  the environment variables, and that CI runs it on PostgreSQL 17.

## Roadmap item addressed

postgres-ci. Honors: a GitHub Actions Postgres service container on one Python
version, SQLite jobs stay, and the service image is postgres:17.
