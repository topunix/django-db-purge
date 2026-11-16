# otel-spans

## Goal

Emit one OpenTelemetry span per MCP tool call, as an optional `[otel]` extra,
so an operator whose host application already configures OpenTelemetry sees
dbpurge tool calls in their traces. Without the extra, the server behaves
exactly as it does today.

## Scope

In:

- `setup.py`: add an `otel` extra that installs `opentelemetry-api` only
  (lower bound `>=1.20`, below 2). No SDK, no exporter.
- A new module `dbpurge/telemetry.py` with no fastmcp import. It imports
  `opentelemetry.trace` inside a try block. If the import fails, it provides a
  no-op span helper so callers need no branching. When the API is present it
  obtains the tracer through `opentelemetry.trace.get_tracer("dbpurge", ...)`
  at call time (not once at import), so a provider the host sets after import
  is honored. The package never calls `set_tracer_provider`, never creates a
  `TracerProvider`, and never configures an exporter.
- Wrap each of the three tools in `purge_mcp_server.py` (`list_policies`,
  `preview_purge`, `execute_purge`) in exactly one span named
  `dbpurge.list_policies`, `dbpurge.preview_purge`, and
  `dbpurge.execute_purge` respectively. The span covers the whole tool body,
  including the rejected and error paths.
- Span attributes, and only these: `dbpurge.policy_name` (the policy name as
  received; omitted for list_policies), `dbpurge.outcome` (the same outcome
  string the call log records: `listed`, `issued`, `consumed`, `rejected`,
  `error`), and `dbpurge.row_count` (set only when the call log has a row
  count). Set the outcome and row count from the same place the existing
  `_log_call` record is emitted so the span and the log record cannot
  disagree.
- No param values, params dicts, confirmation tokens, reasons, or exception
  messages on spans or span events (exception messages can echo agent input).
  For `rejected` and `error` outcomes, set the span status to ERROR with no
  description and do not call `record_exception`. Exceptions still propagate
  unchanged.
- `.github/workflows/ci.yml`: add one `test` matrix leg on Python 3.12 that
  installs `.[mcp,otel]` plus `opentelemetry-sdk` (test-only), and have the
  lint job install `.[mcp,otel]` so mypy resolves the opentelemetry imports.
  Existing legs keep running without the otel extra.
- README: a short subsection under "MCP server" describing the extra, the span
  names and attributes, that param values are never recorded, and that the
  host application configures the SDK and exporter.

Out:

- Configuring any SDK, exporter, or propagator. Metrics and logs signals.
- Spans for the db_purge cron command or for internal helpers (one span per
  tool call only, no child spans).
- Any change to tool signatures, docstrings' safety statements, return values,
  the call log record, or the preview, token, execute handshake.
- Adding `opentelemetry-sdk` to any extra or to `install_requires`.
- The fastmcp 4.x mypy breakage on master (see Deviations if it blocks
  checks).
- docs/roadmap.md.

## Files touched

- setup.py
- dbpurge/telemetry.py (new)
- dbpurge/management/commands/purge_mcp_server.py
- tests/test_telemetry.py (new)
- .github/workflows/ci.yml
- README.md
- pyproject.toml (only if a mypy override is strictly needed; any ignore names
  a specific code and carries a one-line reason)
- docs/tasks/otel-spans.md (this file)

## Acceptance criteria

- `setup.py` has `extras_require` key `otel` whose only requirement is
  `opentelemetry-api` (with a version bound). The `mcp` extra is unchanged.
- `grep -rn "set_tracer_provider\|TracerProvider\|opentelemetry.sdk\|Exporter" dbpurge/`
  returns nothing.
- `grep -n fastmcp dbpurge/telemetry.py` returns nothing.
- With opentelemetry-sdk installed, tests using an `InMemorySpanExporter`
  show: each tool call yields exactly one span with the tool's name; the
  attributes are exactly a subset of `dbpurge.policy_name`, `dbpurge.outcome`,
  `dbpurge.row_count`; outcome and row count match the call log for success,
  rejected (including a bad token and a row cap refusal), and error paths;
  rejected and error spans have status ERROR; no attribute value or event on
  any span contains a param value or the confirmation token.
- A test asserts that with the opentelemetry API unavailable (simulated by
  patching the telemetry module, so it runs on every CI leg), all three tools
  return the same results and emit the same call log records as before, and
  no exception is raised. This test runs whether or not the extra is
  installed.
- Telemetry tests that need the SDK skip cleanly when it is not installed, so
  `python runtests.py` passes on every existing CI leg.
- `python runtests.py` passes with `.[mcp]` only, and with `.[mcp,otel]` plus
  `opentelemetry-sdk`.
- `ruff check .` and `ruff format --check .` pass. `mypy` adds no errors
  beyond those already present on origin/master (compare outputs).
- `.github/workflows/ci.yml` has a test leg installing `.[mcp,otel]` and
  `opentelemetry-sdk`, and the lint job installs the otel extra.
- No em dashes in README or docstrings added by this change.
- `git diff --stat origin/master` lists only files under Files touched.

## Tests to add

`tests/test_telemetry.py`, Django `TestCase` with the existing toy models and
the `stale_samples` policy:

- Span per tool: name and attributes for list_policies, preview_purge, and a
  successful execute_purge (outcome `consumed`, row count equals deleted
  count).
- Rejected paths: unknown policy, out-of-bounds params, invalid token, row cap
  refusal at preview. Outcome `rejected`, status ERROR.
- Error path: an unexpected exception inside the tool body yields outcome
  `error`, status ERROR, and the exception still propagates.
- No param value or token appears in any span attribute or event.
- Extra absent: tools behave identically and nothing raises.

The SDK allows the global tracer provider to be set once per process. Set it
once at module or class setup and clear the in-memory exporter between tests.

## Docs to update

- README.md, new subsection under "MCP server" (near "Call log"): the `[otel]`
  extra, span names, attributes, privacy rule for params, and that the host
  configures the SDK and exporter.

## Roadmap item addressed

otel-spans. Honors: the [otel] extra installs opentelemetry-api only and the
package configures no SDK or exporter; one span per tool call with the three
names; attributes are policy name, outcome, and row count, and param values
are not recorded; behavior without the extra is unchanged and a test asserts
it; tests use the in-memory span exporter from opentelemetry-sdk as a
test-only dependency.
