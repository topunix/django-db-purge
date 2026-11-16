"""
Optional OpenTelemetry spans for the MCP tools.

This module imports only the OpenTelemetry API, never the SDK, and never
configures a provider or exporter: the host application does that. When the
API is not installed, every helper here is a no-op, so callers need no
branching and behavior is unchanged.

Spans carry three attributes at most: the policy name, the outcome and the
row count. Param values, confirmation tokens, refusal reasons and exception
messages are never recorded, because they can echo agent input.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import metadata
from types import ModuleType

_trace: ModuleType | None
try:
    from opentelemetry import trace as _trace_module

    _trace = _trace_module
except ImportError:
    _trace = None

# Outcomes that mark a span as failed. Matches the call log's WARNING and
# ERROR records.
_FAILED_OUTCOMES = frozenset({"rejected", "error"})


def _package_version() -> str | None:
    try:
        return metadata.version("django-db-purge")
    except metadata.PackageNotFoundError:
        return None


@contextmanager
def tool_span(tool: str, policy_name: str | None = None) -> Iterator[None]:
    """
    Wrap one tool call in a span named dbpurge.<tool>.

    The tracer is fetched on every call so a provider the host installs after
    import is honored. Exceptions propagate unchanged and are not recorded on
    the span, since their messages can echo agent input.
    """
    if _trace is None:
        yield
        return
    tracer = _trace.get_tracer("dbpurge", _package_version())
    with tracer.start_as_current_span(
        f"dbpurge.{tool}",
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        if policy_name is not None:
            span.set_attribute("dbpurge.policy_name", policy_name)
        yield


def record_outcome(outcome: str, row_count: int | None = None) -> None:
    """
    Set the outcome and row count on the current tool span.

    Called from the same place as the call log record, so the span and the
    log cannot disagree. A rejected or error outcome sets the span status to
    ERROR with no description.
    """
    if _trace is None:
        return
    span = _trace.get_current_span()
    span.set_attribute("dbpurge.outcome", outcome)
    if row_count is not None:
        span.set_attribute("dbpurge.row_count", row_count)
    if outcome in _FAILED_OUTCOMES:
        span.set_status(_trace.Status(_trace.StatusCode.ERROR))
