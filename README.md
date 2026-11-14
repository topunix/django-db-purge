---

## Django Database Purge

django-db-purge deletes expired database records based on configurable retention policies, run as a scheduled Django management command. It also ships an MCP server so an AI agent can purge records by operator-defined policy. The operator decides what each named policy selects, the agent only picks a policy and supplies bounded params, and every deletion goes through a preview, token, execute handshake. Row age is one kind of policy among several.

### Features:

- **Flexible Retention Policy**: Define your own retention policy to determine which records should be purged from the database.
- **Efficient Data Management**: Easily manage the size of your database by removing outdated or unnecessary records.
- **Customizable**: Adapt the command to suit your project's specific requirements and database structure.
- **Safe**: Built-in safeguards to prevent accidental data loss, ensuring that only the intended records are purged.
- **Agent-safe MCP tools**: Optionally let an AI agent purge records through operator-defined named policies. The agent selects a policy by name and supplies only the bounded params it declares, never a model, a column, or a filter, and a preview, token, execute handshake blocks deletion without a matching prior preview.
- **Policies beyond row age**: On the MCP path a policy can select rows by status, by a numeric threshold, or by an age or an age window. Age is one kind of policy, not the only one.
- **Row-set bound deletion**: A preview binds the exact rows it matched. Execution deletes only those rows, by primary key, and rejects the call outright if the row set drifted in between, so a concurrent write can never widen a purge.

## Scheduled purging (cron)

### How to Use:
1. Install django-db-purge by running:
```bash
pip install django-db-purge
```
2. Include 'dbpurge' in your INSTALLED_APPS settings. 
3. Add a `DB_PURGE_RETENTION_POLICIES` list to your project's `settings.py`, based on your requirements. Below is a guide on how to set up the retention policies:

    #### 1. `app_name`

    - **Description**: Name of the Django app containing the model.
    - **Example**: `my_django_app`

    #### 2. `model_name`

    - **Description**: Name of the Django model from which records will be deleted.
    - **Example**: `MyModel`

    #### 3. `time_based_column_name`

    - **Description**: Name of the column in the model that contains the timestamp or datetime field used for determining the age of records.
    - **Example**: `created_at`

    #### 4. `data_retention_num_seconds`

    - **Description**: Time duration in seconds for which records will be retained before deletion.
    - **Example**: `2592000` (for 30 days)

    #### Example:

    ```python
    # settings.py
    DB_PURGE_RETENTION_POLICIES = [
        {
            'app_name': 'my_django_app',
            'model_name': 'MyModel',
            'time_based_column_name': 'created_at',
            'data_retention_num_seconds': 2592000,  # 30 days in seconds
        },
        # Add more retention policies as needed
    ]
    ```
    If `DB_PURGE_RETENTION_POLICIES` is not set, the command falls back to a placeholder policy that will fail validation until you configure it, so there's no risk of silently deleting the wrong table.
4. Then, either periodically call the db_purge management command (e.g., via a system cronjob), or install and configure django-cron.

---

## MCP server

django-db-purge also ships an MCP server that exposes operator-defined purge policies to an AI agent, with guardrails so an agent can never delete rows without a human-verifiable preview step. It runs inside Django as a management command and speaks the MCP protocol over stdio, so it works with Claude Desktop, the MCP Inspector, or any other MCP-capable host. The server makes no LLM API calls of its own: the host runs the model, and this process only exposes deterministic, schema-validated tools.

### Installation

The MCP server needs `fastmcp`, which is an optional extra, not part of the base install:

```bash
pip install "django-db-purge[mcp]"
```

If `fastmcp` is not installed, running `purge_mcp_server` fails immediately with a `CommandError` pointing back at this install command, rather than a raw import traceback.

### Tools

- **`list_policies()`**
  Read-only. Returns each policy's `name`, `description`, `model`, `action` (always `"delete"`), effective `max_rows`, and its params. Each param has a `name` and a `type`. An `age_seconds` param also lists its `op` (`lte` selects rows older than the age, `gte` rows newer) and `min` and `max`. A `number` param lists `op`, `min`, `max`, and `integer_only`, which is true when a float such as `5.0` will be rejected. A `choice` param lists its `choices`. It never returns a policy's `select`, its `exclude`, or the fields its params filter on. An empty list means nothing is purgeable.

- **`preview_purge(policy_name, params=None)`**
  Read-only. Loads the named policy, validates `params` against it before touching the database, counts the matching rows, and returns up to 5 sample rows (the primary key plus the current value of each param, keyed by param name, never the full row), a `confirmation_token`, and `token_expires_at`. A match count above the policy's effective row cap is refused and no token is issued. Performs no deletion.

- **`execute_purge(policy_name, confirmation_token, params=None)`**
  Deletes the rows a `preview_purge` call matched, but only with that call's `confirmation_token`. Any unknown token, expired token, or token issued for a different policy or different params fails with the same "invalid or expired confirmation token" error, so a caller can't distinguish which of those it was. If the matched row set has changed since the preview, the call is rejected outright.

`params` must contain exactly the params the policy declares: a missing or extra key is rejected, and there are no defaults. A policy that declares none takes `params` omitted, `null`, or `{}`. Each value must match its param's type and bounds (or be one of its choices) exactly, and is checked as the client sent it, so a JSON `true` is never accepted as a number. Nothing in a request can name a model, a column, or a filter.

### Safety handshake

There is no path to deletion without a prior, matching preview:

1. Call `preview_purge` to see what would be deleted and get back a `confirmation_token`.
2. Call `execute_purge` with that token and the identical parameters, before it expires (5 minutes).

Tokens are single-use: a successful `execute_purge` consumes the token, so it cannot be replayed. Tokens are also bound to the policy name and the exact validated params that produced them, so changing either invalidates the token even if it hasn't expired. A token presented with a different policy or different params is used up by that attempt, so the correct call afterwards is rejected too and needs a fresh preview. Once a token matches, a failure that isn't the caller's fault, such as a row-cap breach, a row-set change below, or a database error, reinstates it so a retry with the same token can still succeed.

A token binds the exact set of rows the preview matched, not just the parameters. `execute_purge` deletes only those rows, by primary key, so a row that starts matching the policy after the preview is never deleted. Before deleting, it re-runs the policy's filter (its `select`, its `exclude`, and the params) and compares a fingerprint of the matched rows against the preview's; if the row set has changed, the call is rejected with a distinct error naming both row counts, and you need a fresh `preview_purge`. The fingerprint covers primary keys only by default, so a row that is modified but still matches is still deleted. Set `DB_PURGE_MCP_FINGERPRINT_FIELDS` to include specific column values in it and reject in-place modifications too. See [DESIGN.md](DESIGN.md) for the full rationale, including the cascade limitation.

### Settings

- **`DB_PURGE_MCP_POLICIES`**
  The purge policies an agent may select, as a dict of policy name to definition. Unset or empty means nothing is purgeable. Names match `^[a-z][a-z0-9_]{0,63}$`. Here is one temporal policy, which purges by row age, and one non-temporal policy, which purges by status and a numeric threshold:

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

  - `model` (required): `"app_label.ModelName"`, which must also be in `DB_PURGE_MCP_ALLOWED_MODELS`.
  - `description` (required): what the policy is for. The agent sees it in `list_policies`.
  - `action` (optional): only `"delete"`, which is the default.
  - `select` and `exclude` (optional): `Q` objects that only you write. The agent never sees or supplies them, and neither may be empty. A policy needs a `select` or at least one param.
  - `params` (optional): zero to two entries, each with a `name` (of the same form as a policy name, and not `pk`) and a `type`:
    - `age_seconds`: an integer number of seconds. `field` is a `DateField` or `DateTimeField`. `op` is `lte` (the default, rows at least that old) or `gte` (rows no older than that). Needs integer `min` and `max` with `0 < min <= max`.
    - `number`: an int or a float, never a bool, NaN, or infinity. `field` is an integer, float, or decimal field. `op` is required, one of `lt`, `lte`, `gt`, `gte`. Needs finite `min <= max`. On an integer field, `min` and `max` must be integers and a float value is rejected too.
    - `choice`: the value must equal one entry of `choices` exactly. `choices` is a non-empty list of strings (for a character field) or of integers (for an integer field), with no repeats. Filters `field` by exact match.
  - `max_rows` (optional): a positive integer no greater than `DB_PURGE_MCP_MAX_ROWS`. The effective cap is this value if set, else `DB_PURGE_MCP_MAX_ROWS`.

  A param's `field` must be a plain column on the policy's model: no `__`, not the primary key, and not a relation. Two params may share a field only as an opposite-direction pair of the same type, such as `gte` and `lte`, which defines a window. A window that is inverted or empty is rejected at preview, and equal bounds are valid only when both ends are inclusive.

  Policies are validated whenever they are loaded, which is on every tool call and once when the server starts. Any violation raises `ImproperlyConfigured`, and `purge_mcp_server` then refuses to start with a `CommandError` that carries the detail. A policy whose `select` or `exclude` names a field that does not exist fails the same way. Django reads settings once at startup, so restart the server after changing them.

- **`DB_PURGE_MCP_ALLOWED_MODELS`**
  List of models that may be purged via MCP, in `"app_label.ModelName"` format (e.g. `["tests.SampleRecord"]`). Matched case-insensitively, so `"tests.samplerecord"` also works. Defaults to an empty list, meaning nothing is purgeable until you configure it. Every policy's `model` must be listed, which is checked when policies load and again on every tool call.

- **`DB_PURGE_MCP_MAX_ROWS`**
  Maximum number of matching rows an `execute_purge` call may delete, re-checked at execute time even if the preview was under the cap. Must be a positive integer. Defaults to `10000`. A policy may set a lower `max_rows` of its own, and a preview whose match count exceeds the effective cap is refused. This bounds the rows a policy matches, not cascade fan-out: `ON DELETE CASCADE` relations can remove additional related rows beyond this cap. It also bounds the size of the row-set fingerprint a preview stores.

- **`DB_PURGE_MCP_FINGERPRINT_FIELDS`**
  Optional, per model, opt-in. Maps a model label to the extra columns to include in that model's row-set fingerprint, e.g. `{"tests.SampleRecord": ["label"]}`. Model labels are matched case-insensitively, like `DB_PURGE_MCP_ALLOWED_MODELS`. Defaults to an empty dict, meaning fingerprints cover primary keys only, so rows modified in place between preview and execute are still deleted as long as they still match the filter. Name columns here to have `execute_purge` reject those modifications as well. There is no assumption that a model has an `updated_at` or version column: configure whichever columns matter to you.

### Running the server

Inside your project's virtualenv, with `dbpurge` in `INSTALLED_APPS`:

```bash
python manage.py purge_mcp_server
```

This boots Django (settings, ORM, app registry), validates `DB_PURGE_MCP_POLICIES`, and then serves the three tools above over stdio.

### Claude Desktop configuration

Add an entry to Claude Desktop's `claude_desktop_config.json`, pointing at your project's virtualenv Python and `manage.py`:

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

Use the venv's Python directly (not a bare `python`), since `fastmcp` and your project's dependencies need to be importable.

### Inspecting the server

To poke at the tools interactively, run the server through the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python manage.py purge_mcp_server
```

Run this from inside your project's virtualenv (activated, or with that venv's `python` on `PATH`), so the `python` the Inspector spawns is the one with Django and `fastmcp` installed.

A healthy `tools/list` response shows all three tools: `list_policies`, `preview_purge`, and `execute_purge`, each with a JSON schema derived from its type hints.

### Running the tests

From a bare checkout, with this package installed with the `mcp` extra (`pip install -e ".[mcp]"`), no separate install step for Django or `fastmcp` is required:

```bash
python runtests.py
```

This runs the full Django test suite, including token lifecycle, allowlist, and row-cap coverage, against an in-memory sqlite database.

To exercise the server the way a real MCP client would, over an actual stdio subprocess:

```bash
python tests/e2e_stdio.py
```

This seeds a temporary sqlite database, spawns `python -m django purge_mcp_server`, and drives a full list, preview, execute, and replay-rejected round trip over JSON-RPC.

### Contributions:

Contributions are welcome! If you encounter any issues or have suggestions for improvements, please submit an issue or pull request on GitHub.

### License:

This project is licensed under the MIT License.

---
