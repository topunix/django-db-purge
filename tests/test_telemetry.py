from datetime import timedelta
from unittest import skipIf
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from dbpurge import telemetry
from dbpurge.management.commands import purge_mcp_server
from tests.models import SampleRecord

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.trace import StatusCode
except ImportError:
    trace = None

skip_without_otel = skipIf(trace is None, "opentelemetry-sdk is not installed")

POLICY = "stale_samples"
PARAMS = {"older_than_seconds": 3600}
ALLOWED_ATTRIBUTES = {"dbpurge.policy_name", "dbpurge.outcome", "dbpurge.row_count"}

EXPORTER = None
if trace is not None:
    # The SDK lets the global provider be set once per process, so it is set
    # here and the exporter is cleared between tests.
    EXPORTER = InMemorySpanExporter()
    _provider = TracerProvider()
    _provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
    trace.set_tracer_provider(_provider)


def make_old_records(count):
    for i in range(count):
        SampleRecord.objects.create(
            created_at=timezone.now() - timedelta(days=10, minutes=i), label=str(i)
        )


@skip_without_otel
class TestToolSpans(TestCase):
    def setUp(self):
        EXPORTER.clear()
        self.addCleanup(EXPORTER.clear)

    def only_span(self, name):
        spans = EXPORTER.get_finished_spans()
        self.assertEqual([s.name for s in spans], [name])
        span = spans[0]
        self.assertLessEqual(set(span.attributes), ALLOWED_ATTRIBUTES)
        return span

    def assert_error_status(self, span):
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertIsNone(span.status.description)
        self.assertEqual(span.events, ())

    def test_list_policies_span(self):
        purge_mcp_server.list_policies()

        span = self.only_span("dbpurge.list_policies")
        self.assertEqual(
            dict(span.attributes),
            {"dbpurge.outcome": "listed", "dbpurge.row_count": 1},
        )
        self.assertNotEqual(span.status.status_code, StatusCode.ERROR)

    def test_preview_span(self):
        make_old_records(2)

        purge_mcp_server.preview_purge(POLICY, PARAMS)

        span = self.only_span("dbpurge.preview_purge")
        self.assertEqual(
            dict(span.attributes),
            {
                "dbpurge.policy_name": POLICY,
                "dbpurge.outcome": "issued",
                "dbpurge.row_count": 2,
            },
        )

    def test_execute_span_row_count_is_the_deleted_count(self):
        make_old_records(3)
        token = purge_mcp_server.preview_purge(POLICY, PARAMS)["confirmation_token"]
        EXPORTER.clear()

        result = purge_mcp_server.execute_purge(POLICY, token, PARAMS)

        span = self.only_span("dbpurge.execute_purge")
        self.assertEqual(
            dict(span.attributes),
            {
                "dbpurge.policy_name": POLICY,
                "dbpurge.outcome": "consumed",
                "dbpurge.row_count": result["deleted_count"],
            },
        )
        self.assertEqual(result["deleted_count"], 3)

    def assert_rejected(self, call, name, row_count=None):
        with (
            self.assertLogs("dbpurge.mcp", "WARNING") as logs,
            self.assertRaises(purge_mcp_server.ToolError),
        ):
            call()
        span = self.only_span(name)
        record = logs.records[0]
        self.assertEqual(span.attributes["dbpurge.outcome"], "rejected")
        self.assertEqual(span.attributes["dbpurge.outcome"], record.outcome)
        self.assertEqual(span.attributes.get("dbpurge.row_count"), record.row_count)
        self.assertEqual(span.attributes.get("dbpurge.row_count"), row_count)
        self.assert_error_status(span)
        return span

    def test_unknown_policy_is_rejected(self):
        span = self.assert_rejected(
            lambda: purge_mcp_server.preview_purge("nope", PARAMS),
            "dbpurge.preview_purge",
        )
        self.assertEqual(span.attributes["dbpurge.policy_name"], "nope")

    def test_out_of_bounds_params_are_rejected(self):
        self.assert_rejected(
            lambda: purge_mcp_server.preview_purge(
                POLICY, {"older_than_seconds": 10**12}
            ),
            "dbpurge.preview_purge",
        )

    def test_invalid_token_is_rejected(self):
        self.assert_rejected(
            lambda: purge_mcp_server.execute_purge(POLICY, "bogus-token", PARAMS),
            "dbpurge.execute_purge",
        )

    def test_row_cap_refusal_at_preview_is_rejected(self):
        make_old_records(2)
        with override_settings(DB_PURGE_MCP_MAX_ROWS=1):
            self.assert_rejected(
                lambda: purge_mcp_server.preview_purge(POLICY, PARAMS),
                "dbpurge.preview_purge",
                row_count=2,
            )

    def test_unexpected_exception_is_error_and_propagates(self):
        with (
            patch.object(
                purge_mcp_server, "_preview_loaded", side_effect=KeyError("secret")
            ),
            self.assertLogs("dbpurge.mcp", "ERROR") as logs,
            self.assertRaises(KeyError),
        ):
            purge_mcp_server.preview_purge(POLICY, PARAMS)

        span = self.only_span("dbpurge.preview_purge")
        self.assertEqual(span.attributes["dbpurge.outcome"], "error")
        self.assertEqual(logs.records[0].outcome, "error")
        self.assert_error_status(span)

    def test_no_param_value_or_token_appears_on_any_span(self):
        make_old_records(1)
        params = {"older_than_seconds": 3600123}
        token = purge_mcp_server.preview_purge(POLICY, params)["confirmation_token"]
        purge_mcp_server.execute_purge(POLICY, token, params)
        with self.assertRaises(purge_mcp_server.ToolError):
            purge_mcp_server.execute_purge(POLICY, token, params)

        spans = EXPORTER.get_finished_spans()
        self.assertEqual(len(spans), 3)
        for span in spans:
            texts = [str(v) for v in span.attributes.values()]
            texts += [str(e) for e in span.events]
            texts.append(str(span.status.description))
            for text in texts:
                self.assertNotIn("3600123", text)
                self.assertNotIn(token, text)


class TestWithoutOpenTelemetry(TestCase):
    """Runs on every CI leg: the API is simulated as absent."""

    def test_tools_behave_the_same_and_log_the_same(self):
        make_old_records(2)
        with (
            patch.object(telemetry, "_trace", None),
            self.assertLogs("dbpurge.mcp", "INFO") as logs,
        ):
            listing = purge_mcp_server.list_policies()
            preview = purge_mcp_server.preview_purge(POLICY, PARAMS)
            result = purge_mcp_server.execute_purge(
                POLICY, preview["confirmation_token"], PARAMS
            )
            with self.assertRaises(purge_mcp_server.ToolError):
                purge_mcp_server.execute_purge(POLICY, "bogus", PARAMS)

        self.assertEqual([p["name"] for p in listing], [POLICY])
        self.assertEqual(preview["row_count"], 2)
        self.assertEqual(result["deleted_count"], 2)
        self.assertEqual(
            [(r.outcome, r.row_count) for r in logs.records],
            [("listed", 1), ("issued", 2), ("consumed", 2), ("rejected", None)],
        )
