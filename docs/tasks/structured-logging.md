# structured-logging

## Goal

Emit exactly one stdlib `logging` record per MCP tool call, at the point where
a confirmation token is issued, consumed, or rejected (and, for list_policies,
where the policy list is returned or refused). Each record carries structured
fields as `LogRecord` attributes, so an operator or a test can read who called
what, with which params, how many rows were involved, and how it ended,
without parsing message text.

## Scope

In:

- A dedicated logger, `dbpurge.mcp`, defined in
  `dbpurge/management/commands/purge_mcp_server.py`, that emits one record per
  call to each of the three tools: list_policies, preview_purge,
  execute_purge.
- Each record carries these attributes, passed with `extra=`:
  - `policy_name`: the policy_name argument as received (str), or None for
    list_policies.
  - `params`: the validated params dict when validation succeeded, else the
    params as received (None when omitted). None for list_policies.
  - `row_count`: int or None. preview: the matched count (also on a row cap
    refusal, where it is known). execute: the deleted count on success, else
    None. list_policies: the number of policies returned, else None.
  - `outcome`: one of `"listed"` (list_policies succeeded), `"issued"`
    (preview_purge issued a token), `"consumed"` (execute_purge deleted and
    consumed its token), `"rejected"` (any PurgePolicyError, including
    PurgeRowSetChangedError, an unknown policy, invalid params, a token
    mismatch or expiry, and a row cap breach), or `"error"` (any other
    exception, which is re-raised unchanged).
  - `client_name` and `client_version`: the `clientInfo.name` and
    `clientInfo.version` the MCP client sent in its `initialize` request, read
    from the active FastMCP request context. Both are None when there is no
    active request context (a direct Python call, as in most tests) or when
    fastmcp is not installed. Reading them must never raise.
- On execute `"consumed"` records, an additional `duration_seconds` attribute
  (float), so the CLAUDE.md requirement that destructive actions log
  parameters, counts, and timing stays met once the existing free-text
  execute log line is folded into this record.
- Level: INFO for `"listed"`, `"issued"`, `"consumed"`. WARNING for
  `"rejected"`. ERROR for `"error"`.
- The message text names the tool and the outcome (for example
  `"execute_purge rejected"`), with the reason for a rejection, so the plain
  stderr formatter stays useful. The tool's error returned to the agent is
  unchanged: in particular, the opaque TOKEN_ERROR_MESSAGE stays identical for
  every token failure.
- The record is emitted from the `@mcp.tool` functions (the layer that already
  catches PurgePolicyError and raises ToolError), so it covers every exit path
  of a call, including rejections that happen before the token is looked up.
- Remove the existing free-text `logger.info` lines in
  preview_purge_candidates and execute_purge_candidates, whose content moves
  into the structured record. The `logger.debug` delete breakdown stays on the
  module logger.
- A short DESIGN.md section stating that `client_name` and `client_version`
  come from the MCP `initialize` handshake, identify the client application
  (for example Claude Desktop or MCP Inspector), are self-reported and
  unauthenticated, and do not identify the model that drove the call.

Out:

- A field named for, or claiming to hold, the agent model (Rejected in the
  roadmap entry).
- A DB-backed audit model (Rejected for now, see audit-model).
- Changing the stderr formatter into a JSON formatter, or adding any handler
  configuration beyond what configure_stderr_logging already does.
- OpenTelemetry spans (otel-spans).
- Any change to tool signatures, tool docstrings' safety statements, error
  messages returned to the agent, or the db_purge cron command.
- docs/roadmap.md.

## Files touched

- dbpurge/management/commands/purge_mcp_server.py
- tests/test_purge_mcp_server.py
- DESIGN.md
- README.md
- docs/tasks/structured-logging.md (this file)

## Acceptance criteria

- `grep -n 'getLogger("dbpurge.mcp")' dbpurge/management/commands/purge_mcp_server.py`
  finds the logger definition.
- No `extra=` key passed to the `dbpurge.mcp` logger is named for a model
  (checked by reading `_log_call` and its callers). The existing `"model"` key
  in list_policies output is not a log field and is unaffected.
- The record message carries the fields, so the default stderr formatter shows
  them: `<tool> <outcome> policy=<name> params=<repr> rows=<n> took=<s>s: <reason>`.
  Any part whose value is None is omitted. params are rendered with `repr` so
  newlines in unvalidated input cannot forge log lines. Every extra attribute
  is still passed unchanged. A test asserts the consumed execute message
  contains the policy name, row count and duration.
- A test asserts that a rejected execute_purge (invalid token) emits exactly
  one `dbpurge.mcp` record with `outcome == "rejected"`, `policy_name`,
  `params`, and `row_count is None`, and that the ToolError message is still
  TOKEN_ERROR_MESSAGE.
- Tests assert one record with the expected outcome and fields for: a
  successful list_policies (`"listed"`), a successful preview (`"issued"`,
  `row_count` equals the preview's row_count), a successful execute
  (`"consumed"`, `row_count` equals deleted_count, `duration_seconds` present),
  a preview refused by the row cap (`"rejected"` with the matched count), and
  a preview with invalid params (`"rejected"`, params as received).
- A test through an in-process `fastmcp.Client(..., client_info=...)` (or the
  equivalent fastmcp 3.x API for setting clientInfo) asserts the record's
  `client_name` and `client_version` match what the client sent. It is skipped
  when fastmcp is absent, like the existing TestRealToolLayer tests.
- A test asserts a direct (no request context) tool call logs
  `client_name is None` and `client_version is None`.
- `grep -n 'initialize' DESIGN.md` finds the new section, and it states that
  client identity is not model identity.
- No em dashes (U+2014) in any added line:
  `git diff origin/master | grep '^+' | grep -c $'\u2014'` prints 0.
- `python runtests.py` passes.
- `ruff check .`, `ruff format --check .`, and `mypy` pass.

## Tests to add

In tests/test_purge_mcp_server.py, a new test class (Django TestCase with the
existing toy models and policy fixtures) using `self.assertLogs("dbpurge.mcp")`
to cover each acceptance criterion above, plus one fastmcp-gated test for
client identity through the real tool layer.

## Docs to update

- DESIGN.md: the client identity section described above.
- README.md: a short note in the MCP server section naming the `dbpurge.mcp`
  logger, its fields, and its outcomes, so an operator knows what to route.

## Roadmap item addressed

structured-logging. Verify marker: a test on master asserts a rejected execute
emits a log record with an outcome field.
