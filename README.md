# django-db-purge

[![PyPI](https://img.shields.io/pypi/v/django-db-purge)](https://pypi.org/project/django-db-purge/)
[![CI](https://github.com/topunix/django-db-purge/actions/workflows/ci.yml/badge.svg)](https://github.com/topunix/django-db-purge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Guarded database purging for Django. An MCP server lets an AI agent delete
rows through policies the operator defines: the agent picks a policy by name
and supplies bounded params, and nothing is deleted without a prior preview of
exactly those rows. A separate management command purges rows by age on a
schedule.

> **Status:** this README describes `master`, which is ahead of PyPI. The
> policy interface below is unreleased. For the published 1.3.0 package, see
> the [1.3.0 README](https://github.com/topunix/django-db-purge/blob/v1.3.0/README.md).

## Features

- **Preview, token, execute handshake**: `execute_purge` only accepts a
  single-use token from a matching `preview_purge` call, valid for 5 minutes.
- **Operator-defined policies**: the agent never names a model, a column, or a
  filter. A policy can select by status, a numeric threshold, a choice, or age.
- **Row-set bound deletion**: execution deletes the previewed rows by primary
  key and is rejected if the matching set changed in between.
- **Bounded by default**: an empty model allowlist until you configure it, and
  a row cap checked at both preview and execute.
- **Scheduled purging**: an age-based management command for cron.

The package has two entry points with separate settings:

| Entry point | Command | Setting | Selects rows by |
| --- | --- | --- | --- |
| MCP server | `purge_mcp_server` | `DB_PURGE_MCP_POLICIES` | Named policies |
| Scheduled purging | `db_purge` | `DB_PURGE_RETENTION_POLICIES` | Age only |


## Requirements

- Python 3.10 or newer. CI runs on 3.10 and 3.12.
- CI installs the latest Django release, unpinned. Older Django versions are
  untested.

## Installation

The PyPI commands install 1.3.0, which predates the policy interface described
below:

```bash
pip install django-db-purge            # 1.3.0, scheduled purging only
pip install "django-db-purge[mcp]"     # 1.3.0, adds the MCP server (fastmcp)
```

To install `master` with the `mcp` extra from GitHub:

```bash
pip install "django-db-purge[mcp] @ git+https://github.com/topunix/django-db-purge.git"
```

Add `'dbpurge'` to `INSTALLED_APPS`.

If `fastmcp` is not installed, `purge_mcp_server` exits with a `CommandError`
that points back to the install command above, instead of a raw import
traceback.

## MCP server

The server runs inside Django as a management command and speaks MCP over
stdio, so it works with any MCP host. It makes no LLM API calls of its own.

### Try it

From a checkout of this repo, seed a demo database and start the server:

```bash
pip install -e ".[mcp]"
python tests/seed_demo.py
DJANGO_SETTINGS_MODULE=tests.demo_settings python -m django purge_mcp_server
```

Run directly, the server logs a startup banner to stderr and then waits
silently on stdio for an MCP client, with no prompt. To talk to it, start it
through the MCP Inspector instead, from the same checkout:

```bash
npx @modelcontextprotocol/inspector \
  -e DJANGO_SETTINGS_MODULE=tests.demo_settings \
  python -m django purge_mcp_server
```

Pass the settings module with `-e`. The Inspector does not forward variables
from your shell to the server it spawns.

### Example session

This session was captured from a real run against the demo database, calling
the tools through an in-process `fastmcp.Client`, with JSON reformatted for
width only. Each call shows its arguments, then its output.

`list_policies` takes no arguments. It returns each policy's name,
description, model, action, max_rows, and params, and never the model fields
the params filter on.

```json
[
  {
    "name": "stale_samples",
    "description": "Sample records older than the given age.",
    "model": "tests.SampleRecord",
    "action": "delete",
    "max_rows": 10000,
    "params": [
      {
        "name": "older_than_seconds",
        "type": "age_seconds",
        "op": "lte",
        "min": 1,
        "max": 315360000
      }
    ]
  },
  {
    "name": "demo_labelled_samples",
    "description": "Sample records with one of the demo labels.",
    "model": "tests.SampleRecord",
    "action": "delete",
    "max_rows": 10000,
    "params": [
      {
        "name": "label",
        "type": "choice",
        "choices": [
          "demo-1d",
          "demo-12d",
          "demo-23d"
        ]
      }
    ]
  }
]
```

`preview_purge` validates the params, counts the matching rows, and returns a
sample and a confirmation token. Nothing is deleted. Sample rows show the
current value of each param's column, keyed by the param name. The column name
itself is never returned.

Arguments:

```json
{
  "policy_name": "stale_samples",
  "params": {"older_than_seconds": 5184000}
}
```

Output:

```json
{
  "row_count": 4,
  "sample_rows": [
    {
      "pk": 27,
      "older_than_seconds": "2026-09-11T00:42:31.253411+00:00"
    },
    {
      "pk": 28,
      "older_than_seconds": "2026-08-31T00:42:31.253411+00:00"
    },
    {
      "pk": 29,
      "older_than_seconds": "2026-08-20T00:42:31.253411+00:00"
    },
    {
      "pk": 30,
      "older_than_seconds": "2026-08-09T00:42:31.253411+00:00"
    }
  ],
  "confirmation_token": "Z12aEN_Ka5gxW-wsIJ-QYqTfbGZvMIdx87ecg-ClLoQ",
  "token_expires_at": "2026-11-17T00:47:34.664656+00:00"
}
```

`execute_purge` with that token and the same params deletes the previewed
rows.

Arguments:

```json
{
  "policy_name": "stale_samples",
  "confirmation_token": "Z12aEN_Ka5gxW-wsIJ-QYqTfbGZvMIdx87ecg-ClLoQ",
  "params": {"older_than_seconds": 5184000}
}
```

Output:

```json
{
  "deleted_count": 4,
  "duration_seconds": 0.0029575670000099308
}
```

Replaying the same execute call, or previewing a value outside the policy's
bounds, is rejected.

```text
execute_purge (same arguments again): invalid or expired confirmation token
preview_purge (older_than_seconds 0): param 'older_than_seconds' is out of bounds (1 to 315360000)
```

### Defining policies

Policies live in `DB_PURGE_MCP_POLICIES`, a dict of policy name to definition.
Unset or empty means nothing is purgeable. Names match
`^[a-z][a-z0-9_]{0,63}$`. Here is one temporal policy, which purges by row age,
and one non-temporal policy, which purges by status and a numeric threshold:

```python
# settings.py
from django.db.models import Q

DB_PURGE_MCP_ALLOWED_MODELS = ["jobs.Job", "billing.Invoice"]

DB_PURGE_MCP_POLICIES = {
    "stale_failed_jobs": {
        "model": "jobs.Job",
        "description": "Failed or cancelled jobs past retention.",
        "select": Q(status__in=["failed", "cancelled"]),
        "exclude": Q(status="under_review"),
        "params": [
            {
                "name": "older_than_seconds",
                "type": "age_seconds",
                "field": "created_at",
                "min": 2592000,  # at least 30 days
                "max": 315360000,  # at most 10 years
            },
        ],
        "max_rows": 5000,
    },
    "small_draft_invoices": {
        "model": "billing.Invoice",
        "description": "Draft invoices at or below a total, in a chosen currency.",
        "select": Q(status="draft"),
        "params": [
            {
                "name": "max_total",
                "type": "number",
                "field": "total",
                "op": "lte",
                "min": 0,
                "max": 100,
            },
            {
                "name": "currency",
                "type": "choice",
                "field": "currency",
                "choices": ["USD", "EUR"],
            },
        ],
    },
}
```

A policy has these keys:

- `model` (required): `"app_label.ModelName"`, which must also be in
  `DB_PURGE_MCP_ALLOWED_MODELS`.
- `description` (required): what the policy is for. The agent sees it in
  `list_policies`.
- `action` (optional): only `"delete"`, which is the default.
- `select` and `exclude` (optional): `Q` objects that only you write. The agent
  never sees or supplies them, and neither may be empty. A policy needs a
  `select` or at least one param.
- `params` (optional): zero to two entries, each with a `name` (of the same
  form as a policy name, and not `pk`) and a `type`:
  - `age_seconds`: an integer number of seconds. `field` is a `DateField` or
    `DateTimeField`. `op` is `lte` (the default, rows at least that old) or
    `gte` (rows no older than that). Needs integer `min` and `max` with
    `0 < min <= max`.
  - `number`: an int or a float, never a bool, NaN, or infinity. `field` is an
    integer, float, or decimal field. `op` is required, one of `lt`, `lte`,
    `gt`, `gte`. Needs finite `min <= max`. On an integer field, `min` and
    `max` must be integers and a float value is rejected too.
  - `choice`: the value must equal one entry of `choices` exactly. `choices` is
    a non-empty list of strings (for a character field) or of integers (for an
    integer field), with no repeats. Filters `field` by exact match.
- `max_rows` (optional): a positive integer no greater than
  `DB_PURGE_MCP_MAX_ROWS`. The effective cap is this value if set, else
  `DB_PURGE_MCP_MAX_ROWS`.

A param's `field` must be a plain column on the policy's model: no `__`, not
the primary key, and not a relation. Two params may share a field only as an
opposite-direction pair of the same type, such as `gte` and `lte`, which
defines a window. A window that is inverted or empty is rejected at preview,
and equal bounds are valid only when both ends are inclusive.

Policies are validated whenever they are loaded, which is on every tool call
and once when the server starts. Any violation raises `ImproperlyConfigured`,
and `purge_mcp_server` then refuses to start with a `CommandError` that carries
the detail. A policy whose `select` or `exclude` names a field that does not
exist fails the same way. Django reads settings once at startup, so restart the
server after changing them.

### Running the server

Inside your project's virtualenv, with `'dbpurge'` in `INSTALLED_APPS`:

```bash
python manage.py purge_mcp_server
```

This boots Django (settings, ORM, app registry), validates
`DB_PURGE_MCP_POLICIES`, and then serves the three [tools](#tools) over stdio.

### Host configuration

**Claude Desktop**: add an entry to `claude_desktop_config.json`.

```json
{
  "mcpServers": {
    "django-db-purge": {
      "command": "/path/to/your/project/.venv/bin/python",
      "args": ["/path/to/your/project/manage.py", "purge_mcp_server"],
      "cwd": "/path/to/your/project"
    }
  }
}
```

Use the venv's Python directly (not a bare `python`), since `fastmcp` and your
project's dependencies need to be importable.

**Claude Code**:

```bash
claude mcp add --transport stdio django-db-purge \
  -- /path/to/your/project/.venv/bin/python \
  /path/to/your/project/manage.py purge_mcp_server
```

### Inspecting the server

To poke at the tools interactively, run the server through the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python manage.py purge_mcp_server
```

Run this from inside your project's virtualenv (activated, or with that venv's
`python` on `PATH`), so the `python` the Inspector spawns is the one with
Django and `fastmcp` installed.

A healthy `tools/list` response shows all three tools, `list_policies`,
`preview_purge`, and `execute_purge`, each with a JSON schema derived from its
type hints.

### Tools

- **`list_policies()`**
  Read-only. Returns each policy's `name`, `description`, `model`, `action`
  (always `"delete"`), effective `max_rows`, and its params. Each param has a
  `name` and a `type`. An `age_seconds` param also lists its `op` (`lte`
  selects rows older than the age, `gte` rows newer) and `min` and `max`. A
  `number` param lists `op`, `min`, `max`, and `integer_only`, which is true
  when a float such as `5.0` will be rejected. A `choice` param lists its
  `choices`. It never returns a policy's `select`, its `exclude`, or the fields
  its params filter on. An empty list means nothing is purgeable.

- **`preview_purge(policy_name, params=None)`**
  Read-only. Loads the named policy, validates `params` against it before
  touching the database, counts the matching rows, and returns up to 5 sample
  rows (the primary key plus the current value of each param, keyed by param
  name, never the full row), a `confirmation_token`, and `token_expires_at`. A
  match count above the policy's effective row cap is refused and no token is
  issued. Performs no deletion.

- **`execute_purge(policy_name, confirmation_token, params=None)`**
  Deletes the rows a `preview_purge` call matched, but only with that call's
  `confirmation_token`. Any unknown token, expired token, or token issued for a
  different policy or different params fails with the same "invalid or expired
  confirmation token" error, so a caller can't distinguish which of those it
  was. If the matched row set has changed since the preview, the call is
  rejected outright.

`params` must contain exactly the params the policy declares: a missing or
extra key is rejected, and there are no defaults. A policy that declares none
takes `params` omitted, `null`, or `{}`. Each value must match its param's type
and bounds (or be one of its choices) exactly, and is checked as the client
sent it, so a JSON `true` is never accepted as a number. Nothing in a request
can name a model, a column, or a filter.

### Safety handshake

There is no path to deletion without a prior, matching preview:

1. Call `preview_purge` to see what would be deleted and get back a
   `confirmation_token`.
2. Call `execute_purge` with that token and the identical parameters, before it
   expires (5 minutes).

Tokens are single-use: a successful `execute_purge` consumes the token, so it
cannot be replayed. Tokens are also bound to the policy name and the exact
validated params that produced them, so changing either invalidates the token
even if it hasn't expired. A token presented with a different policy or
different params is used up by that attempt, so the correct call afterwards is
rejected too and needs a fresh preview. Once a token matches, a failure that
isn't the caller's fault, such as a row-cap breach, a changed row set, or a
database error, reinstates it so a retry with the same token can still succeed.

A token binds the exact set of rows the preview matched, not just the
parameters. `execute_purge` deletes only those rows, by primary key, so a row
that starts matching the policy after the preview is never deleted. Before
deleting, it re-runs the policy's filter (its `select`, its `exclude`, and the
params) and compares a fingerprint of the matched rows against the preview's.
If the row set has changed, the call is rejected with a distinct error naming
both row counts, and you need a fresh `preview_purge`.

Two limits apply to that guarantee:

- **In-place modification.** The fingerprint covers primary keys only by
  default, so a row that is modified but still matches is still deleted. Set
  `DB_PURGE_MCP_FINGERPRINT_FIELDS` to include specific column values in it and
  reject in-place modifications too.
- **Cascades.** The row cap bounds the rows a policy matches, not cascade
  fan-out: `ON DELETE CASCADE` relations can remove additional related rows
  beyond the cap.

For the full rationale, see
[DESIGN.md](https://github.com/topunix/django-db-purge/blob/master/DESIGN.md),
including the
[cascade limitation](https://github.com/topunix/django-db-purge/blob/master/DESIGN.md#adr-3-row-set-fingerprinting).

### Settings reference

Four settings configure the MCP server. For the shape of a policy, see
[Defining policies](#defining-policies).

- **`DB_PURGE_MCP_ALLOWED_MODELS`**
  List of models that may be purged via MCP, in `"app_label.ModelName"` format
  (e.g. `["tests.SampleRecord"]`). Matched case-insensitively. Defaults to an
  empty list, meaning nothing is purgeable until you configure it. Every
  policy's `model` must be listed, which is checked when policies load and
  again on every tool call.

- **`DB_PURGE_MCP_POLICIES`**
  The purge policies an agent may select, as a dict of policy name to
  definition. Unset or empty means nothing is purgeable.

- **`DB_PURGE_MCP_MAX_ROWS`**
  Maximum number of matching rows an `execute_purge` call may delete,
  re-checked at execute time even if the preview was under the cap. Must be a
  positive integer. Defaults to `10000`. A policy may set a lower `max_rows` of
  its own, and a preview whose match count exceeds the effective cap is
  refused. Cascades are not counted (see [Safety handshake](#safety-handshake)).
  It also bounds the size of the row-set fingerprint a preview stores.

- **`DB_PURGE_MCP_FINGERPRINT_FIELDS`**
  Optional and opt-in. Maps a model label to the extra columns to include in
  that model's row-set fingerprint, e.g. `{"tests.SampleRecord": ["label"]}`.
  Model labels are matched case-insensitively. Defaults to an empty dict, so
  fingerprints cover primary keys only (see
  [Safety handshake](#safety-handshake)). No model is assumed to have an
  `updated_at` or version column: configure whichever columns matter to you.

### Call log

Each tool call that reaches the tool writes exactly one record to the
`dbpurge.mcp` logger (a call FastMCP rejects during argument validation writes
none), so an operator can route it to an audit sink. Fields are attributes on
the `LogRecord`, not parsed from the message: `policy_name`, `params`,
`row_count`, `outcome`, `client_name`, `client_version`, and, on `consumed`,
`duration_seconds`. `outcome` is `listed`, `issued` (preview issued a token),
`consumed` (execute deleted), `rejected` (a policy, params, token or row cap
refusal, logged at WARNING), or `error` (an unexpected exception, logged at
ERROR). `client_name` and `client_version` are what the client self-reports at
connection time; they do not identify the model.

### OpenTelemetry spans

Install the optional extra to get one span per tool call in your traces:

```bash
pip install "django-db-purge[mcp,otel]"
```

The `otel` extra exists only on `master`, so this command needs the GitHub
install shown under [Installation](#installation) (the 1.3.0 package has no
such extra):

```bash
pip install "django-db-purge[mcp,otel] @ git+https://github.com/topunix/django-db-purge.git"
```

The extra installs `opentelemetry-api` only. The spans are named
`dbpurge.list_policies`, `dbpurge.preview_purge` and `dbpurge.execute_purge`,
and carry at most three attributes: `dbpurge.policy_name` (absent for
`list_policies`), `dbpurge.outcome` (the same value the call log records) and
`dbpurge.row_count` (only when the call log has one). A `rejected` or `error`
outcome sets the span status to ERROR with no description. Param values,
confirmation tokens, refusal reasons and exception messages are never recorded
on spans or span events, because they can echo agent input.

django-db-purge never configures a tracer provider, SDK, exporter or
propagator. Your host application does that, and with none configured the spans
are no-ops. fastmcp already depends on `opentelemetry-api`, so the API is
usually importable even without the extra; the extra just pins a supported
range. Without the API, the server behaves exactly as before.

## Scheduled purging (cron)

The `db_purge` command deletes rows older than a retention period. It selects
by age only and does not read `DB_PURGE_MCP_POLICIES`. It has no dry run, no
row cap, and no confirmation step: each run deletes every matching row.

Add `DB_PURGE_RETENTION_POLICIES` to `settings.py`:

```python
# settings.py
DB_PURGE_RETENTION_POLICIES = [
    {
        'app_name': 'my_django_app',
        'model_name': 'MyModel',
        'time_based_column_name': 'created_at',
        'data_retention_num_seconds': 2592000,  # 30 days in seconds
    },
]
```

| Key | Meaning | Example |
| --- | --- | --- |
| `app_name` | Django app containing the model | `my_django_app` |
| `model_name` | Model to delete from | `MyModel` |
| `time_based_column_name` | Date or datetime field that gives a row's age | `created_at` |
| `data_retention_num_seconds` | Seconds a row is kept before deletion | `2592000` |

If the setting is absent, the command falls back to a placeholder policy that
fails validation, so it cannot silently delete the wrong table.

Run it on a schedule, for example nightly at 03:00:

```cron
0 3 * * * cd /path/to/your/project && .venv/bin/python manage.py db_purge
```

## Known limits

- **Cascades are not capped.** See [Safety handshake](#safety-handshake).
- **Tokens live in process memory.** A restart discards them, and they are not
  shared between processes. See
  [ADR 2](https://github.com/topunix/django-db-purge/blob/master/DESIGN.md#adr-2-what-the-token-binds-and-how-it-is-stored).
- **Fingerprints cover primary keys by default.** See
  [Safety handshake](#safety-handshake).
- **stdio transport only.** There is no HTTP transport.
- **Scheduled purging has none of the MCP guardrails.** See
  [Scheduled purging](#scheduled-purging-cron).

## Development

### Running the tests

From a bare checkout, install the package with the `mcp` extra
(`pip install -e ".[mcp]"`). Nothing else is needed, because that also installs
Django and `fastmcp`:

```bash
python runtests.py
```

This runs the full Django test suite, including token lifecycle, allowlist, and
row-cap coverage, against an in-memory sqlite database.

To run the suite against PostgreSQL instead (needs a running server and a
driver, for example `pip install "psycopg[binary]"`), set
`DBPURGE_TEST_DB_ENGINE=postgresql`. Connection settings come from
`DBPURGE_TEST_DB_NAME`, `DBPURGE_TEST_DB_USER`, `DBPURGE_TEST_DB_PASSWORD`,
`DBPURGE_TEST_DB_HOST`, and `DBPURGE_TEST_DB_PORT`, defaulting to `postgres`,
`postgres`, `postgres`, `localhost`, and `5432`:

```bash
DBPURGE_TEST_DB_ENGINE=postgresql python runtests.py
```

This is the only configuration where the row-locking (`select_for_update`) path
runs against a real database. CI runs it on PostgreSQL 17.

To exercise the server the way a real MCP client would, over an actual stdio
subprocess:

```bash
python tests/e2e_stdio.py
```

This seeds a temporary sqlite database, spawns
`python -m django purge_mcp_server`, and drives a full list, preview, execute,
and replay-rejected round trip over JSON-RPC.

### Adversarial suite

A red-team suite attacks the three MCP tools with forged and replayed tokens,
parameter confusion, row-set drift, scope escapes, row-cap bypasses and
injection payloads. Each attack is either proven blocked or recorded as a
finding with a severity and a proposed fix. It needs `pytest` and
`pytest-django` and does not run under `python runtests.py`:

```bash
pytest -m adversarial tests/adversarial
```

The threat model, how findings are recorded and the latest results are in
[evals/README.md](evals/README.md).

## Contributing

Issues and pull requests are welcome on GitHub.

## License

MIT. See [LICENSE](LICENSE).
