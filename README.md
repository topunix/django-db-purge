---

## Django Database Purge

The Django Database Purge management command is a tool for efficiently removing unwanted records from your Django project's database based on a specified retention policy. This command helps you keep your database clean and optimized by permanently deleting records that are no longer needed.

### Features:

- **Flexible Retention Policy**: Define your own retention policy to determine which records should be purged from the database.
- **Efficient Data Management**: Easily manage the size of your database by removing outdated or unnecessary records.
- **Customizable**: Adapt the command to suit your project's specific requirements and database structure.
- **Safe**: Built-in safeguards to prevent accidental data loss, ensuring that only the intended records are purged.

### How to Use:
1. Install django-db-purge by running:
```bash
pip install django-db-purge
```
2. Include 'dbpurge' in your INSTALLED_APPS settings. 
3. Locate the `db_purge.py` file in the `management/commands` directory of the Django dbpurge app.
4. Add your own values to the retention policies dictionary in the `db_purge.py` file, based on your requirements. Below is a guide on how to set up the retention policies:

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
    retention_policies = [
        {
            'app_name': 'my_django_app',
            'model_name': 'MyModel',
            'time_based_column_name': 'created_at',
            'data_retention_num_seconds': 2592000,  # 30 days in seconds
        },
        # Add more retention policies as needed
    ]
    ```
5. Then, either periodically call the db_purge management command (e.g., via a system cronjob), or install and configure django-cron.

---

## MCP server

django-db-purge also ships an MCP server that exposes the same purge logic to an AI agent, with guardrails so an agent can never delete rows without a human-verifiable preview step. It runs inside Django as a management command and speaks the MCP protocol over stdio, so it works with Claude Desktop, the MCP Inspector, or any other MCP-capable host. The server makes no LLM API calls of its own: the host runs the model, and this process only exposes deterministic, schema-validated tools.

### Tools

- **`list_purge_candidates()`**
  Read-only. Introspects installed models and returns every app, model, and `DateField`/`DateTimeField` column, so an agent can discover valid inputs for the other two tools.

- **`preview_purge(app_name, model_name, time_column, retention_seconds)`**
  Read-only. Validates the policy against live schema, counts matching rows, and returns up to 5 sample rows (primary key and the time column only, never the full row), a `confirmation_token`, and `token_expires_at`. Performs no deletion.

- **`execute_purge(app_name, model_name, time_column, retention_seconds, confirmation_token)`**
  Deletes the matching rows, but only with a `confirmation_token` from a matching `preview_purge` call. Any unknown token, expired token, or parameter mismatch fails with the same "invalid or expired confirmation token" error, so a caller can't distinguish which of those it was.

### Safety handshake

There is no path to deletion without a prior, matching preview:

1. Call `preview_purge` to see what would be deleted and get back a `confirmation_token`.
2. Call `execute_purge` with that token and the identical parameters, before it expires (5 minutes).

Tokens are single-use: a successful `execute_purge` consumes the token, so it cannot be replayed. Tokens are also bound to the exact parameter tuple that produced them, so changing any argument invalidates the token even if it hasn't expired. If `execute_purge` fails for a reason that isn't the caller's fault, such as a row-cap breach below, the token is reinstated so a retry with the same token can still succeed.

### Settings

- **`DB_PURGE_MCP_ALLOWED_MODELS`**
  List of models that may be purged via MCP, in `"app_label.ModelName"` format (e.g. `["tests.SampleRecord"]`). Matched case-insensitively, so `"tests.samplerecord"` also works. Defaults to an empty list, meaning nothing is purgeable until you configure it. Enforced on both `preview_purge` and `execute_purge`.

- **`DB_PURGE_MCP_MAX_ROWS`**
  Maximum number of matching rows an `execute_purge` call may delete, re-checked at execute time even if the preview was under the cap. Defaults to `10000`. This bounds the rows matched by `time_column`, not cascade fan-out: `ON DELETE CASCADE` relations can remove additional related rows beyond this cap.

### Running the server

Inside your project's virtualenv, with `dbpurge` in `INSTALLED_APPS`:

```bash
python manage.py purge_mcp_server
```

This boots Django (settings, ORM, app registry) and then serves the three tools above over stdio.

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

A healthy `tools/list` response shows all three tools: `list_purge_candidates`, `preview_purge`, and `execute_purge`, each with a JSON schema derived from its type hints.

### Running the tests

From a bare checkout, with Django and `fastmcp` installed (`pip install Django fastmcp`), no separate install step for this package itself is required:

```bash
python runtests.py
```

This runs the full Django test suite, including token lifecycle, allowlist, and row-cap coverage, against an in-memory sqlite database.

To exercise the server the way a real MCP client would, over an actual stdio subprocess:

```bash
python tests/e2e_stdio.py
```

This seeds a temporary sqlite database, spawns `python -m django purge_mcp_server`, and drives a full preview, execute, and reuse-rejected round trip over JSON-RPC.

### Contributions:

Contributions are welcome! If you encounter any issues or have suggestions for improvements, please submit an issue or pull request on GitHub.

### License:

This project is licensed under the MIT License.

---
