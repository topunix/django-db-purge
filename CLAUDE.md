# CLAUDE.md

## Project

django-db-purge is an open-source Django package for deleting expired
database records based on configurable retention policies. This work
exposes the purge logic as an MCP server so AI agents can invoke it
under explicit safety constraints. Because the operations are
destructive, code quality, commit hygiene, and safety design are part
of the deliverable rather than afterthoughts.

## Goal

Expose the purge logic as MCP tools that an AI agent can discover and
call, with guardrails that prevent an agent from executing destructive
operations without a human-verifiable preview step.

## Architecture (decided, do not relitigate)

- FastMCP 3.x for the server. Not the raw MCP SDK, not FastAPI-MCP.
- The server runs inside Django as a management command:
  python manage.py purge_mcp_server
  This boots Django settings, the ORM, and the app registry, then calls
  mcp.run(). Do not build a standalone script that bypasses Django.
- Transport: stdio first (for Claude Desktop / Inspector). Streamable
  HTTP is a later phase, not part of the initial build.
- No LLM API calls anywhere in this codebase. The host runs the model.
  This server only exposes deterministic, schema-validated tools.
- The MCP build is merged. The db_purge cron command remains the
  deterministic fallback path and must keep working, but it is in
  scope for changes now (e.g. sourcing retention policies from
  Django settings), not frozen.

## Tool surface (exactly three tools)

1. list_purge_candidates()
   Introspects installed models via apps.get_models(). Returns apps,
   models, and their DateTimeField/DateField columns. Read-only.

2. preview_purge(app_name, model_name, time_column, retention_seconds)
   Validates inputs against the real schema (reuse validate_policy
   logic). Runs the retention filter, returns the matching row count,
   a small sample of rows, and a short-lived confirmation token.
   Performs no deletion.

3. execute_purge(app_name, model_name, time_column, retention_seconds,
   confirmation_token)
   Deletes only if the token matches a recent preview with identical
   parameters. Token mismatch, expiry, or parameter drift is a hard
   error, not a warning.

## Safety requirements

- Preview, token, execute handshake is mandatory. There is no path to
  deletion without a prior matching preview.
- Confirmation tokens expire (suggest 5 minutes) and are bound to the
  exact parameter set that produced them.
- Max-rows cap per execute call, configurable via Django settings with
  a conservative default (suggest 10000). Exceeding it fails loudly.
- Model allowlist, configurable via Django settings. Empty allowlist
  means no model may be purged via MCP.
- Every tool call validates app, model, and column against live
  introspection before doing anything.
- All destructive actions log parameters, counts, and timing.

## Code conventions

- Refactor, do not rewrite. delete_expired_records splits into a
  preview half (filter + count) and an execute half (delete). The
  existing validate_policy becomes the shared guard.
- Type hints on all tool functions. FastMCP derives schemas from them,
  so signatures and docstrings are part of the API contract.
- Tests for the token lifecycle (valid, expired, parameter drift),
  the allowlist, and the row cap. Use Django TestCase with a toy model.
- Work on a feature branch. Small, well-messaged commits. The history
  should explain why each change was made, not just what changed.
- Plain, readable code over cleverness. A reviewer should grasp the
  safety model from the tool docstrings alone.
- Lint and type checks run in CI and must pass locally first:
  ruff check .
  ruff format --check .
  mypy
- Every per-file ignore, noqa, and type: ignore names a specific code
  and carries a one-line reason. No blanket ignores.

## Documentation

- Update the README with an MCP section: what the tools do, the safety
  handshake, Claude Desktop config snippet, and Inspector instructions.
- Never use em dashes anywhere in documentation or docstrings. Use
  commas, parentheses, or periods.

## Working style

- Before any multi-file change, state a 2 to 3 sentence execution plan
  and wait for confirmation.
- When showing proposed edits in discussion, show only new or changed
  functions with comments marking unchanged code.
- No co-author or attribution trailers in commit messages.

## Roadmap

- The roadmap lives at docs/roadmap.md.
- Check an item's Verify marker against master before acting on it.
- Only the maintainer reorders or adds roadmap items. Propose changes,
  do not apply them.
- Task specs live in docs/tasks/<slug>.md and are deleted after the work
  lands.
