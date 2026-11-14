import asyncio
import inspect
import json
from dataclasses import replace
from datetime import date, timedelta
from unittest import skipIf
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.db import connection
from django.db.models import Q, QuerySet
from django.test import TestCase, override_settings
from django.utils import timezone

from dbpurge.management.commands import purge_mcp_server
from dbpurge.management.commands.purge_mcp_server import (
    _TOKENS,
    MISSING_FASTMCP_ERROR,
    TOKEN_ERROR_MESSAGE,
    PurgePolicyError,
    PurgeRowSetChangedError,
    describe_policies,
    execute_purge_candidates,
    preview_purge_candidates,
    token_is_valid,
)
from tests.models import PolicyNote, PolicyRecord, SampleRecord

try:
    import fastmcp
except ImportError:
    fastmcp = None

skip_without_fastmcp = skipIf(fastmcp is None, "fastmcp is not installed")

POLICY = "stale_samples"
PARAMS = {"older_than_seconds": 3600}


def expire_token(token):
    _TOKENS[token] = replace(
        _TOKENS[token], expires_at=timezone.now() - timedelta(seconds=1)
    )


class TestPreviewPurgeCandidates(TestCase):
    def setUp(self):
        self.old_record = SampleRecord.objects.create(
            created_at=timezone.now() - timedelta(days=10), label="old"
        )

    def test_issues_a_valid_token_bound_to_the_policy_and_params(self):
        preview = preview_purge_candidates(POLICY, PARAMS)

        self.assertEqual(preview["row_count"], 1)
        self.assertEqual(
            preview["sample_rows"],
            [
                {
                    "pk": self.old_record.pk,
                    "older_than_seconds": self.old_record.created_at.isoformat(),
                }
            ],
        )
        self.assertTrue(token_is_valid(preview["confirmation_token"], POLICY, PARAMS))

    def test_caps_sample_rows_at_five(self):
        SampleRecord.objects.all().delete()
        for i in range(7):
            SampleRecord.objects.create(
                created_at=timezone.now() - timedelta(days=10, minutes=i), label=str(i)
            )

        preview = preview_purge_candidates(POLICY, PARAMS)

        self.assertEqual(preview["row_count"], 7)
        self.assertEqual(len(preview["sample_rows"]), 5)

    def test_performs_no_deletes(self):
        preview_purge_candidates(POLICY, PARAMS)

        self.assertEqual(SampleRecord.objects.count(), 1)

    def test_token_rejected_once_expired(self):
        preview = preview_purge_candidates(POLICY, PARAMS)
        token = preview["confirmation_token"]
        expire_token(token)

        self.assertFalse(token_is_valid(token, POLICY, PARAMS))

    def test_token_rejected_on_parameter_drift(self):
        preview = preview_purge_candidates(POLICY, PARAMS)
        token = preview["confirmation_token"]

        self.assertFalse(token_is_valid(token, POLICY, {"older_than_seconds": 7200}))

    def test_unknown_token_rejected(self):
        self.assertFalse(token_is_valid("not-a-real-token", POLICY, PARAMS))

    def test_denied_for_a_model_outside_the_allowlist(self):
        with (
            override_settings(DB_PURGE_MCP_ALLOWED_MODELS=[]),
            self.assertLogs("dbpurge.policies", "ERROR"),
            self.assertRaises(PurgePolicyError),
        ):
            preview_purge_candidates(POLICY, PARAMS)

    def test_allowlist_matches_regardless_of_casing(self):
        # Django's lowercase model_name form is easy to copy from the shell,
        # so it must work as well as the class-cased label.
        with override_settings(DB_PURGE_MCP_ALLOWED_MODELS=["tests.samplerecord"]):
            preview_purge_candidates(POLICY, PARAMS)


class TestExecutePurgeCandidates(TestCase):
    def setUp(self):
        self.old_record = SampleRecord.objects.create(
            created_at=timezone.now() - timedelta(days=10), label="old"
        )
        preview = preview_purge_candidates(POLICY, PARAMS)
        self.token = preview["confirmation_token"]

    def _error_message_for(self, token, params=PARAMS, policy=POLICY):
        with self.assertRaises(PurgePolicyError) as ctx:
            execute_purge_candidates(policy, token, params)
        return str(ctx.exception)

    def test_deletes_matching_rows_and_returns_the_real_total(self):
        result = execute_purge_candidates(POLICY, self.token, PARAMS)

        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(SampleRecord.objects.count(), 0)

    def test_token_is_single_use(self):
        # This is the retry/double-spend scenario, simulated without
        # real threads: the fix's guarantee is that the token is
        # popped out of _TOKENS atomically with the first call, so a
        # second (or a racing concurrent) call has nothing left to
        # validate against. We can't fork real concurrent requests in
        # a unit test, so the closest feasible proof is asserting the
        # token is truly gone from the store immediately, not just
        # logically rejected, before confirming the second call fails.
        execute_purge_candidates(POLICY, self.token, PARAMS)
        self.assertNotIn(self.token, _TOKENS)

        message = self._error_message_for(self.token)

        self.assertEqual(message, TOKEN_ERROR_MESSAGE)

    def test_denied_for_a_model_outside_the_allowlist(self):
        with (
            override_settings(DB_PURGE_MCP_ALLOWED_MODELS=[]),
            self.assertLogs("dbpurge.policies", "ERROR"),
            self.assertRaises(PurgePolicyError),
        ):
            execute_purge_candidates(POLICY, self.token, PARAMS)

        self.assertEqual(SampleRecord.objects.count(), 1)

    def _add_a_second_matching_row(self):
        return SampleRecord.objects.create(
            created_at=timezone.now() - timedelta(days=11), label="second"
        )

    def test_cap_breach_blocks_delete_and_leaves_token_usable(self):
        self._add_a_second_matching_row()
        with (
            override_settings(DB_PURGE_MCP_MAX_ROWS=1),
            self.assertRaises(PurgePolicyError) as ctx,
        ):
            execute_purge_candidates(POLICY, self.token, PARAMS)

        self.assertIn("exceeding the max", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, PurgeRowSetChangedError)
        self.assertEqual(SampleRecord.objects.count(), 2)
        self.assertTrue(token_is_valid(self.token, POLICY, PARAMS))

    def test_retry_succeeds_after_a_cap_breach_is_resolved(self):
        second = self._add_a_second_matching_row()
        with (
            override_settings(DB_PURGE_MCP_MAX_ROWS=1),
            self.assertRaises(PurgePolicyError),
        ):
            execute_purge_candidates(POLICY, self.token, PARAMS)

        second.delete()
        result = execute_purge_candidates(POLICY, self.token, PARAMS)

        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(SampleRecord.objects.count(), 0)

    def test_allowlist_matches_regardless_of_casing(self):
        with override_settings(DB_PURGE_MCP_ALLOWED_MODELS=["tests.samplerecord"]):
            result = execute_purge_candidates(POLICY, self.token, PARAMS)

        self.assertEqual(result["deleted_count"], 1)

    def test_opaque_message_identical_for_unknown_expired_and_drifted_tokens(self):
        unknown_message = self._error_message_for("not-a-real-token")

        expire_token(self.token)
        expired_message = self._error_message_for(self.token)

        fresh_preview = preview_purge_candidates(POLICY, PARAMS)
        drifted_message = self._error_message_for(
            fresh_preview["confirmation_token"], {"older_than_seconds": 7200}
        )

        self.assertEqual(unknown_message, TOKEN_ERROR_MESSAGE)
        self.assertEqual(expired_message, TOKEN_ERROR_MESSAGE)
        self.assertEqual(drifted_message, TOKEN_ERROR_MESSAGE)


class TestRowSetFingerprinting(TestCase):
    def setUp(self):
        self.old_record = SampleRecord.objects.create(
            created_at=timezone.now() - timedelta(days=10), label="old"
        )

    def test_fingerprint_matches_execute_succeeds(self):
        preview = preview_purge_candidates(POLICY, PARAMS)

        result = execute_purge_candidates(POLICY, preview["confirmation_token"], PARAMS)

        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(SampleRecord.objects.count(), 0)

    def test_row_inserted_into_match_set_rejects_execute(self):
        preview = preview_purge_candidates(POLICY, PARAMS)
        token = preview["confirmation_token"]

        SampleRecord.objects.create(
            created_at=timezone.now() - timedelta(days=11), label="phantom"
        )

        with self.assertRaises(PurgeRowSetChangedError):
            execute_purge_candidates(POLICY, token, PARAMS)

        self.assertEqual(SampleRecord.objects.count(), 2)

    def test_row_removed_from_match_set_rejects_execute(self):
        preview = preview_purge_candidates(POLICY, PARAMS)
        token = preview["confirmation_token"]

        self.old_record.delete()

        with self.assertRaises(PurgeRowSetChangedError):
            execute_purge_candidates(POLICY, token, PARAMS)

    def test_row_mutated_out_of_filter_rejects_execute(self):
        preview = preview_purge_candidates(POLICY, PARAMS)
        token = preview["confirmation_token"]

        self.old_record.created_at = timezone.now()
        self.old_record.save()

        with self.assertRaises(PurgeRowSetChangedError):
            execute_purge_candidates(POLICY, token, PARAMS)

        self.assertEqual(SampleRecord.objects.count(), 1)

    def test_row_mutated_but_still_matching_pk_only_default_execute_succeeds(self):
        preview = preview_purge_candidates(POLICY, PARAMS)
        token = preview["confirmation_token"]

        self.old_record.label = "renamed"
        self.old_record.save()

        result = execute_purge_candidates(POLICY, token, PARAMS)

        self.assertEqual(result["deleted_count"], 1)

    def test_row_mutated_with_fingerprint_fields_configured_rejects_execute(self):
        with override_settings(
            DB_PURGE_MCP_FINGERPRINT_FIELDS={"tests.SampleRecord": ["label"]}
        ):
            preview = preview_purge_candidates(POLICY, PARAMS)
            token = preview["confirmation_token"]

            self.old_record.label = "renamed"
            self.old_record.save()

            with self.assertRaises(PurgeRowSetChangedError):
                execute_purge_candidates(POLICY, token, PARAMS)

    def test_unrelated_writes_do_not_affect_the_fingerprint(self):
        preview = preview_purge_candidates(POLICY, PARAMS)
        token = preview["confirmation_token"]

        # A write to a row that does not, and never did, match the
        # policy must not perturb the fingerprint.
        SampleRecord.objects.create(created_at=timezone.now(), label="unrelated")

        result = execute_purge_candidates(POLICY, token, PARAMS)

        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(SampleRecord.objects.count(), 1)

    def test_fingerprint_mismatch_reinstates_the_token(self):
        # Corrected design decision: drift is not caller error, so it
        # follows the same reinstate-on-failure path as a cap breach
        # rather than permanently burning the token.
        preview = preview_purge_candidates(POLICY, PARAMS)
        token = preview["confirmation_token"]

        self.old_record.delete()

        with self.assertRaises(PurgeRowSetChangedError):
            execute_purge_candidates(POLICY, token, PARAMS)

        self.assertTrue(token_is_valid(token, POLICY, PARAMS))

    def test_execute_succeeds_on_backend_without_select_for_update_support(self):
        # Proves execute still works when the backend can't lock rows
        # (true of SQLite here); it does NOT exercise the gate itself,
        # since has_select_for_update is False so select_for_update()
        # is never called. See test_select_for_update_applied_when_
        # backend_supports_it for that.
        self.assertFalse(connection.features.has_select_for_update)

        preview = preview_purge_candidates(POLICY, PARAMS)

        result = execute_purge_candidates(POLICY, preview["confirmation_token"], PARAMS)

        self.assertEqual(result["deleted_count"], 1)

    def test_select_for_update_applied_when_backend_supports_it(self):
        # SQLite itself can't run SELECT ... FOR UPDATE, so
        # select_for_update() is mocked at the queryset level (as a
        # no-op passthrough) rather than actually exercised against
        # the database; only the has_select_for_update gate and the
        # call are under test here.
        preview = preview_purge_candidates(POLICY, PARAMS)
        token = preview["confirmation_token"]

        def passthrough_select_for_update(self, *args, **kwargs):
            return self

        with (
            patch.object(connection.features, "has_select_for_update", True),
            patch.object(
                QuerySet,
                "select_for_update",
                autospec=True,
                side_effect=passthrough_select_for_update,
            ) as mock_select_for_update,
        ):
            result = execute_purge_candidates(POLICY, token, PARAMS)

        mock_select_for_update.assert_called_once()
        self.assertEqual(result["deleted_count"], 1)


class TestMissingFastMCP(TestCase):
    @patch("dbpurge.management.commands.purge_mcp_server.FastMCP", None)
    def test_handle_raises_a_command_error_pointing_at_the_extra(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("purge_mcp_server")

        self.assertEqual(str(ctx.exception), MISSING_FASTMCP_ERROR)


class TestStartup(TestCase):
    """handle() loads policies once, so bad config stops the server early."""

    def run_command(self, policies):
        fake_mcp = MagicMock()
        with (
            override_settings(DB_PURGE_MCP_POLICIES=policies),
            patch.object(purge_mcp_server, "FastMCP", object()),
            patch.object(purge_mcp_server, "mcp", fake_mcp),
            patch.object(purge_mcp_server, "configure_stderr_logging"),
        ):
            call_command("purge_mcp_server")
        return fake_mcp

    def test_valid_policies_start_the_server(self):
        fake_mcp = self.run_command(POLICIES_BY_NAME)

        fake_mcp.run.assert_called_once_with(transport="stdio")

    def test_no_policies_still_starts_the_server(self):
        fake_mcp = self.run_command({})

        fake_mcp.run.assert_called_once_with(transport="stdio")

    def test_invalid_policy_stops_startup_with_a_command_error(self):
        bad = {"p": {**POLICIES_BY_NAME["failed_rows"], "action": "archive"}}
        fake_mcp = MagicMock()
        with (
            override_settings(DB_PURGE_MCP_POLICIES=bad),
            patch.object(purge_mcp_server, "FastMCP", object()),
            patch.object(purge_mcp_server, "mcp", fake_mcp),
            patch.object(purge_mcp_server, "configure_stderr_logging"),
            self.assertRaises(CommandError) as ctx,
        ):
            call_command("purge_mcp_server")

        self.assertIn("action must be", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, ImproperlyConfigured)
        fake_mcp.run.assert_not_called()

    def test_an_invalid_global_row_cap_stops_startup(self):
        fake_mcp = MagicMock()
        with (
            override_settings(DB_PURGE_MCP_POLICIES={}, DB_PURGE_MCP_MAX_ROWS=0),
            patch.object(purge_mcp_server, "FastMCP", object()),
            patch.object(purge_mcp_server, "mcp", fake_mcp),
            patch.object(purge_mcp_server, "configure_stderr_logging"),
            self.assertRaises(CommandError) as ctx,
        ):
            call_command("purge_mcp_server")

        self.assertIn("DB_PURGE_MCP_MAX_ROWS", str(ctx.exception))
        fake_mcp.run.assert_not_called()


def make_record(**kwargs):
    defaults = {
        "created_at": timezone.now() - timedelta(days=1),
        "due_on": date.today() - timedelta(days=1),
    }
    return PolicyRecord.objects.create(**{**defaults, **kwargs})


def age_param(**overrides):
    return {
        "name": "older_than_seconds",
        "type": "age_seconds",
        "field": "created_at",
        "min": 60,
        "max": 10**8,
        **overrides,
    }


POLICIES_BY_NAME = {
    "failed_rows": {
        "model": "tests.PolicyRecord",
        "description": "Failed rows.",
        "select": Q(status="failed"),
    },
    "other_failed_rows": {
        "model": "tests.PolicyRecord",
        "description": "Failed rows, a second policy with the same shape.",
        "select": Q(status="failed"),
    },
    "old_rows": {
        "model": "tests.PolicyRecord",
        "description": "Rows past an age.",
        "params": [age_param()],
    },
    "high_scores": {
        "model": "tests.PolicyRecord",
        "description": "Rows at or above a score.",
        "params": [
            {
                "name": "min_score",
                "type": "number",
                "field": "score",
                "op": "gte",
                "min": 0,
                "max": 100,
            }
        ],
    },
    "levels_below": {
        "model": "tests.PolicyRecord",
        "description": "Rows below an integer level.",
        "params": [
            {
                "name": "below_level",
                "type": "number",
                "field": "level",
                "op": "lt",
                "min": -10,
                "max": 10,
            }
        ],
    },
    "by_state": {
        "model": "tests.PolicyRecord",
        "description": "Rows in a state.",
        "params": [
            {
                "name": "state",
                "type": "choice",
                "field": "status",
                "choices": ["failed", "cancelled"],
            }
        ],
    },
    "age_window": {
        "model": "tests.PolicyRecord",
        "description": "Rows inside an age window.",
        "params": [
            age_param(name="older_than", min=1, op="lte"),
            age_param(name="newer_than", min=1, op="gte"),
        ],
    },
    "score_window": {
        "model": "tests.PolicyRecord",
        "description": "Rows inside a score window.",
        "params": [
            {
                "name": "low",
                "type": "number",
                "field": "score",
                "op": "gte",
                "min": 0,
                "max": 100,
            },
            {
                "name": "high",
                "type": "number",
                "field": "score",
                "op": "lt",
                "min": 0,
                "max": 100,
            },
        ],
    },
    "unheld_failed": {
        "model": "tests.PolicyRecord",
        "description": "Failed rows that are not on hold.",
        "select": Q(status__in=["failed", "held"]),
        "exclude": Q(status="held"),
    },
    "capped": {
        "model": "tests.PolicyRecord",
        "description": "Failed rows, at most two per purge.",
        "select": Q(status="failed"),
        "max_rows": 2,
    },
    "with_notes": {
        "model": "tests.PolicyRecord",
        "description": "Rows that have an x note.",
        "select": Q(notes__body="x"),
    },
}


class PolicyTestCase(TestCase):
    def setUp(self):
        override = override_settings(DB_PURGE_MCP_POLICIES=POLICIES_BY_NAME)
        override.enable()
        self.addCleanup(override.disable)

    def preview(self, policy, params=None):
        return preview_purge_candidates(policy, params)

    def execute(self, policy, token, params=None):
        return execute_purge_candidates(policy, token, params)

    def assertRejectedWithoutQueries(self, call, *args):
        with self.assertNumQueries(0), self.assertRaises(PurgePolicyError):
            call(*args)


class TestNoPolicies(TestCase):
    def test_every_preview_is_rejected_when_the_setting_is_empty_or_unset(self):
        with override_settings(DB_PURGE_MCP_POLICIES={}):
            self.assertEqual(describe_policies(), [])
            for name in ("failed_rows", "stale_samples", "", "x"):
                with self.subTest(name=name), self.assertRaises(PurgePolicyError):
                    preview_purge_candidates(name, None)
        with override_settings():
            del settings.DB_PURGE_MCP_POLICIES
            self.assertEqual(describe_policies(), [])
            with self.assertRaises(PurgePolicyError):
                preview_purge_candidates("stale_samples", {"older_than_seconds": 100})


class TestPolicyWithoutParams(PolicyTestCase):
    def test_previews_and_executes_with_params_omitted_none_or_empty(self):
        for params in ("omitted", None, {}):
            with self.subTest(params=params):
                row = make_record(status="failed")
                make_record(status="new")
                args = () if params == "omitted" else (params,)

                preview = self.preview("failed_rows", *args)
                result = self.execute(
                    "failed_rows", preview["confirmation_token"], *args
                )

                self.assertEqual(preview["row_count"], 1)
                self.assertEqual(preview["sample_rows"], [{"pk": row.pk}])
                self.assertEqual(result["deleted_count"], 1)
                PolicyRecord.objects.all().delete()

    def test_none_and_empty_params_bind_the_same_token(self):
        make_record(status="failed")
        preview = self.preview("failed_rows", None)

        result = self.execute("failed_rows", preview["confirmation_token"], {})

        self.assertEqual(result["deleted_count"], 1)

    def test_supplying_a_param_is_rejected(self):
        self.assertRejectedWithoutQueries(self.preview, "failed_rows", {"x": 1})


class TestAgeSecondsPolicy(PolicyTestCase):
    def test_previews_and_executes_correctly(self):
        old = make_record(created_at=timezone.now() - timedelta(hours=5))
        make_record(created_at=timezone.now() - timedelta(minutes=5))
        params = {"older_than_seconds": 3600}

        preview = self.preview("old_rows", params)
        result = self.execute("old_rows", preview["confirmation_token"], params)

        self.assertEqual(preview["row_count"], 1)
        self.assertEqual(preview["sample_rows"][0]["pk"], old.pk)
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(PolicyRecord.objects.count(), 1)

    def test_rejects_bad_values_without_touching_the_database(self):
        for value in (59, 10**8 + 1, -1, 3600.0, 3600.5, True, "3600", None):
            with self.subTest(value=value):
                self.assertRejectedWithoutQueries(
                    self.preview, "old_rows", {"older_than_seconds": value}
                )
                self.assertRejectedWithoutQueries(
                    self.execute, "old_rows", "token", {"older_than_seconds": value}
                )

    def test_rejects_undeclared_and_missing_params(self):
        for params in (
            None,
            {},
            {"older_than_days": 5},
            {"older_than_seconds": 100, "x": 1},
        ):
            with self.subTest(params=params):
                self.assertRejectedWithoutQueries(self.preview, "old_rows", params)


class TestNumberPolicy(PolicyTestCase):
    def test_previews_and_executes_correctly(self):
        make_record(score=1.0)
        high = make_record(score=7.5)
        params = {"min_score": 5}

        preview = self.preview("high_scores", params)
        result = self.execute("high_scores", preview["confirmation_token"], params)

        self.assertEqual(preview["row_count"], 1)
        self.assertEqual(preview["sample_rows"], [{"pk": high.pk, "min_score": 7.5}])
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(PolicyRecord.objects.count(), 1)

    def test_rejects_bad_values_without_touching_the_database(self):
        bad = (-0.1, 100.1, True, False, "5", None, float("nan"), float("inf"))
        for value in bad + (float("-inf"),):
            with self.subTest(value=value):
                self.assertRejectedWithoutQueries(
                    self.preview, "high_scores", {"min_score": value}
                )

    def test_integer_field_rejects_floats_and_accepts_ints(self):
        make_record(level=-8)
        make_record(level=-5)
        for value in (-5.5, 5.0):
            with self.subTest(value=value):
                self.assertRejectedWithoutQueries(
                    self.preview, "levels_below", {"below_level": value}
                )
        preview = self.preview("levels_below", {"below_level": -5})
        self.assertEqual(preview["row_count"], 1)


class TestChoicePolicy(PolicyTestCase):
    def test_previews_and_executes_correctly(self):
        failed = make_record(status="failed")
        make_record(status="cancelled")
        params = {"state": "failed"}

        preview = self.preview("by_state", params)
        result = self.execute("by_state", preview["confirmation_token"], params)

        self.assertEqual(preview["sample_rows"], [{"pk": failed.pk, "state": "failed"}])
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(PolicyRecord.objects.get().status, "cancelled")

    def test_rejects_undeclared_values_without_touching_the_database(self):
        for value in ("new", "FAILED", 1, True, None, ["failed"]):
            with self.subTest(value=value):
                self.assertRejectedWithoutQueries(
                    self.preview, "by_state", {"state": value}
                )


class TestWindowPolicy(PolicyTestCase):
    def test_deletes_only_rows_inside_the_window(self):
        now = timezone.now()
        make_record(created_at=now - timedelta(hours=1))
        inside = make_record(created_at=now - timedelta(hours=5))
        make_record(created_at=now - timedelta(hours=20))
        params = {"older_than": 2 * 3600, "newer_than": 10 * 3600}

        preview = self.preview("age_window", params)
        result = self.execute("age_window", preview["confirmation_token"], params)

        self.assertEqual(preview["row_count"], 1)
        self.assertEqual(result["deleted_count"], 1)
        self.assertFalse(PolicyRecord.objects.filter(pk=inside.pk).exists())
        self.assertEqual(PolicyRecord.objects.count(), 2)

    def test_number_window_selects_the_half_open_range(self):
        for score in (1.0, 3.0, 5.0, 7.0):
            make_record(score=score)
        preview = self.preview("score_window", {"low": 3, "high": 7})

        self.assertEqual(preview["row_count"], 2)

    def test_inverted_and_empty_windows_are_rejected_at_preview(self):
        inverted = {"older_than": 10 * 3600, "newer_than": 2 * 3600}
        self.assertRejectedWithoutQueries(self.preview, "age_window", inverted)
        self.assertRejectedWithoutQueries(
            self.preview, "score_window", {"low": 5, "high": 3}
        )
        # Equal bounds with a strict upper op are an empty window.
        self.assertRejectedWithoutQueries(
            self.preview, "score_window", {"low": 5, "high": 5}
        )


class TestExcludeApplied(PolicyTestCase):
    def test_exclude_applies_at_preview_and_execute(self):
        failed = make_record(status="failed")
        make_record(status="held")

        preview = self.preview("unheld_failed")
        result = self.execute("unheld_failed", preview["confirmation_token"])

        self.assertEqual(preview["row_count"], 1)
        self.assertEqual(preview["sample_rows"], [{"pk": failed.pk}])
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(PolicyRecord.objects.get().status, "held")

    def test_a_row_that_becomes_excluded_after_preview_is_drift(self):
        row = make_record(status="failed")
        preview = self.preview("unheld_failed")

        row.status = "held"
        row.save()

        with self.assertRaises(PurgeRowSetChangedError):
            self.execute("unheld_failed", preview["confirmation_token"])
        self.assertEqual(PolicyRecord.objects.count(), 1)


class TestTokenBinding(PolicyTestCase):
    def test_a_token_for_one_policy_is_rejected_for_another(self):
        make_record(status="failed")
        token = self.preview("failed_rows")["confirmation_token"]

        with self.assertRaises(PurgePolicyError) as ctx:
            self.execute("other_failed_rows", token)

        self.assertEqual(str(ctx.exception), TOKEN_ERROR_MESSAGE)
        self.assertEqual(PolicyRecord.objects.count(), 1)

    def test_a_token_is_rejected_for_the_same_policy_with_different_params(self):
        make_record(created_at=timezone.now() - timedelta(days=2))
        token = self.preview("old_rows", {"older_than_seconds": 3600})[
            "confirmation_token"
        ]

        with self.assertRaises(PurgePolicyError) as ctx:
            self.execute("old_rows", token, {"older_than_seconds": 7200})

        self.assertEqual(str(ctx.exception), TOKEN_ERROR_MESSAGE)
        self.assertEqual(PolicyRecord.objects.count(), 1)

    def test_a_token_used_with_another_policy_is_burned(self):
        # A token presented with the wrong policy is popped and not
        # reinstated, so the correct call afterwards fails too.
        row = make_record(status="failed")
        token = self.preview("failed_rows")["confirmation_token"]

        with self.assertRaises(PurgePolicyError):
            self.execute("other_failed_rows", token)
        self.assertNotIn(token, _TOKENS)
        with self.assertRaises(PurgePolicyError) as ctx:
            self.execute("failed_rows", token)

        self.assertEqual(str(ctx.exception), TOKEN_ERROR_MESSAGE)
        self.assertTrue(PolicyRecord.objects.filter(pk=row.pk).exists())

    def test_different_params_burn_the_token_for_the_correct_call(self):
        make_record(created_at=timezone.now() - timedelta(days=2))
        params = {"older_than_seconds": 3600}
        token = self.preview("old_rows", params)["confirmation_token"]

        with self.assertRaises(PurgePolicyError):
            self.execute("old_rows", token, {"older_than_seconds": 7200})
        with self.assertRaises(PurgePolicyError) as ctx:
            self.execute("old_rows", token, params)

        self.assertEqual(str(ctx.exception), TOKEN_ERROR_MESSAGE)
        self.assertEqual(PolicyRecord.objects.count(), 1)

    def test_a_request_rejected_before_the_token_check_leaves_it_alone(self):
        make_record(created_at=timezone.now() - timedelta(days=2))
        params = {"older_than_seconds": 3600}
        token = self.preview("old_rows", params)["confirmation_token"]

        with self.assertRaises(PurgePolicyError):
            self.execute("old_rows", token, {"older_than_seconds": 5})
        with self.assertRaises(PurgePolicyError):
            self.execute("no_such_policy", token, params)

        self.assertEqual(self.execute("old_rows", token, params)["deleted_count"], 1)

    def test_int_and_float_params_bind_different_tokens(self):
        make_record(score=7.0)
        token = self.preview("high_scores", {"min_score": 5})["confirmation_token"]

        with self.assertRaises(PurgePolicyError):
            self.execute("high_scores", token, {"min_score": 5.0})


class TestPolicyMaxRows(PolicyTestCase):
    def test_a_policy_cap_below_the_global_cap_is_enforced_at_preview(self):
        for _ in range(3):
            make_record(status="failed")

        with self.assertRaises(PurgePolicyError) as ctx:
            self.preview("capped")

        self.assertIn("match 3 rows", str(ctx.exception))
        self.assertIn("max of 2", str(ctx.exception))
        self.assertEqual(PolicyRecord.objects.count(), 3)

    def test_the_preview_cap_error_reports_counts_without_echoing_params(self):
        for _ in range(3):
            make_record(created_at=timezone.now() - timedelta(days=5))
        with (
            override_settings(DB_PURGE_MCP_MAX_ROWS=2),
            self.assertRaises(PurgePolicyError) as ctx,
        ):
            self.preview("old_rows", {"older_than_seconds": 3600})
        message = str(ctx.exception)

        self.assertIn("match 3 rows", message)
        self.assertIn("max of 2", message)
        self.assertNotIn("3600", message)
        self.assertNotIn("older_than_seconds", message)

    def test_a_policy_cap_below_the_global_cap_is_enforced_at_execute(self):
        make_record(status="failed")
        make_record(status="failed")
        token = self.preview("capped")["confirmation_token"]
        make_record(status="failed")

        with self.assertRaises(PurgePolicyError) as ctx:
            self.execute("capped", token)

        self.assertIn("max of 2", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, PurgeRowSetChangedError)
        self.assertEqual(PolicyRecord.objects.count(), 3)

    def test_the_global_cap_applies_to_a_policy_without_its_own(self):
        make_record(status="failed")
        make_record(status="failed")
        # "capped" sets a max_rows above 1, which would fail the load, so
        # only the policy without its own cap is configured here.
        only = {"failed_rows": POLICIES_BY_NAME["failed_rows"]}

        with (
            override_settings(DB_PURGE_MCP_POLICIES=only, DB_PURGE_MCP_MAX_ROWS=1),
            self.assertRaises(PurgePolicyError) as ctx,
        ):
            self.preview("failed_rows")

        self.assertIn("match 2 rows", str(ctx.exception))
        self.assertIn("max of 1", str(ctx.exception))


class TestToManyJoin(PolicyTestCase):
    def test_a_join_across_a_to_many_counts_distinct_rows(self):
        record = make_record()
        for _ in range(3):
            PolicyNote.objects.create(record=record, body="x")
        other = make_record()
        PolicyNote.objects.create(record=other, body="x")
        make_record()

        preview = self.preview("with_notes")
        result = self.execute("with_notes", preview["confirmation_token"])

        self.assertEqual(preview["row_count"], 2)
        self.assertEqual(len(preview["sample_rows"]), 2)
        self.assertEqual(PolicyRecord.objects.count(), 1)
        self.assertEqual(result["deleted_count"], 2 + 4)  # rows plus cascaded notes


class TestAgentSuppliedText(PolicyTestCase):
    def test_an_unknown_policy_name_is_never_echoed_or_logged(self):
        name = "evil\nINJECTED line"
        with self.assertNoLogs(level="DEBUG"):
            with self.assertRaises(PurgePolicyError) as preview_ctx:
                preview_purge_candidates(name, None)
            with self.assertRaises(PurgePolicyError) as execute_ctx:
                execute_purge_candidates(name, "token", None)

        for ctx in (preview_ctx, execute_ctx):
            self.assertNotIn("INJECTED", str(ctx.exception))
            self.assertNotIn("\n", str(ctx.exception))

    def test_a_bad_token_string_is_never_echoed_or_logged(self):
        make_record(status="failed")
        with (
            self.assertNoLogs(level="DEBUG"),
            self.assertRaises(PurgePolicyError) as ctx,
        ):
            execute_purge_candidates("failed_rows", "evil\nTOKEN", None)

        self.assertEqual(str(ctx.exception), TOKEN_ERROR_MESSAGE)

    def test_bad_param_text_is_never_echoed_or_logged(self):
        with self.assertNoLogs(level="DEBUG"):
            with self.assertRaises(PurgePolicyError) as ctx:
                preview_purge_candidates("by_state", {"state": "evil\nINJECTED"})
            with self.assertRaises(PurgePolicyError) as extra_ctx:
                preview_purge_candidates("by_state", {"evil\nKEY": 1})

        self.assertNotIn("INJECTED", str(ctx.exception))
        self.assertNotIn("KEY", str(extra_ctx.exception))


class TestPreviewShape(PolicyTestCase):
    def test_the_pk_in_a_sample_row_is_the_real_primary_key(self):
        row = make_record(status="failed", score=7.5)

        preview = self.preview("high_scores", {"min_score": 5})

        (sample,) = preview["sample_rows"]
        self.assertEqual(sample["pk"], row.pk)
        self.assertEqual(sample["min_score"], 7.5)

    def test_no_preview_response_uses_a_model_field_name_as_a_key(self):
        row = make_record(status="failed", score=7.0)
        field_names = {f.name for f in PolicyRecord._meta.get_fields()} | {
            f.attname for f in PolicyRecord._meta.concrete_fields
        }
        cases = {
            "old_rows": {"older_than_seconds": 60},
            "high_scores": {"min_score": 5},
            "by_state": {"state": "failed"},
            "age_window": {"older_than": 10, "newer_than": 100000},
            "failed_rows": None,
        }
        row.created_at = timezone.now() - timedelta(days=1)
        row.save()

        for policy, params in cases.items():
            with self.subTest(policy=policy):
                preview = self.preview(policy, params)
                keys = set(preview)
                for sample in preview["sample_rows"]:
                    keys |= set(sample)
                self.assertTrue(preview["sample_rows"])
                self.assertFalse(keys & field_names, keys & field_names)

    def test_sample_values_are_rendered_as_json_data(self):
        make_record(status="failed", amount="12.50")
        policy = {
            "model": "tests.PolicyRecord",
            "description": "d",
            "params": [
                {
                    "name": "max_amount",
                    "type": "number",
                    "field": "amount",
                    "op": "lte",
                    "min": 0,
                    "max": 100,
                },
                age_param(name="older_than", field="due_on", min=1),
            ],
        }
        with override_settings(DB_PURGE_MCP_POLICIES={"p": policy}):
            preview = preview_purge_candidates(
                "p", {"max_amount": 20, "older_than": 10}
            )

        (sample,) = preview["sample_rows"]
        self.assertEqual(sample["max_amount"], "12.50")
        self.assertIsInstance(sample["older_than"], str)
        json.dumps(preview)


FORBIDDEN_ARGUMENTS = {"app_name", "model_name", "time_column", "retention_seconds"}


class TestToolSignatures(TestCase):
    def test_no_function_accepts_a_model_column_or_retention_argument(self):
        for function in (preview_purge_candidates, execute_purge_candidates):
            with self.subTest(function=function.__name__):
                parameters = set(inspect.signature(function).parameters)
                self.assertFalse(parameters & FORBIDDEN_ARGUMENTS)

    def test_execute_takes_the_token_before_the_optional_params(self):
        self.assertEqual(
            list(inspect.signature(execute_purge_candidates).parameters),
            ["policy_name", "confirmation_token", "params"],
        )


@skip_without_fastmcp
class TestRealToolLayer(TestCase):
    """Calls the registered tools through FastMCP, as a client would."""

    def setUp(self):
        override = override_settings(DB_PURGE_MCP_POLICIES=POLICIES_BY_NAME)
        override.enable()
        self.addCleanup(override.disable)

    def call(self, tool, arguments):
        async def run():
            async with fastmcp.Client(purge_mcp_server.mcp) as client:
                return await client.call_tool(tool, arguments, raise_on_error=False)

        return asyncio.run(run())

    def test_the_tool_surface_is_exactly_three_policy_tools(self):
        async def names():
            async with fastmcp.Client(purge_mcp_server.mcp) as client:
                return {tool.name: tool for tool in await client.list_tools()}

        tools = asyncio.run(names())

        self.assertEqual(
            set(tools), {"list_policies", "preview_purge", "execute_purge"}
        )
        for tool in tools.values():
            properties = set(tool.input_schema.get("properties", {}))
            self.assertFalse(properties & FORBIDDEN_ARGUMENTS)
        self.assertEqual(
            set(tools["preview_purge"].input_schema["properties"]),
            {"policy_name", "params"},
        )
        self.assertEqual(
            set(tools["execute_purge"].input_schema["properties"]),
            {"policy_name", "confirmation_token", "params"},
        )
        self.assertEqual(
            tools["execute_purge"].input_schema["required"],
            ["policy_name", "confirmation_token"],
        )

    def test_list_policies_works_through_the_tool_layer(self):
        result = self.call("list_policies", {})

        self.assertFalse(result.is_error)
        names = {info["name"] for info in result.structured_content["result"]}
        self.assertEqual(names, set(POLICIES_BY_NAME))

    def test_json_true_and_false_are_rejected_for_a_number_param(self):
        for value in (True, False):
            with (
                self.subTest(value=value),
                patch.object(purge_mcp_server, "preview_purge_candidates") as preview,
            ):
                result = self.call(
                    "preview_purge",
                    {"policy_name": "high_scores", "params": {"min_score": value}},
                )

            self.assertTrue(result.is_error)
            preview.assert_not_called()

    def test_json_true_is_rejected_for_execute_too(self):
        with patch.object(purge_mcp_server, "execute_purge_candidates") as execute:
            result = self.call(
                "execute_purge",
                {
                    "policy_name": "high_scores",
                    "confirmation_token": "t",
                    "params": {"min_score": True},
                },
            )

        self.assertTrue(result.is_error)
        execute.assert_not_called()

    def test_ints_and_floats_reach_validation_with_their_own_types(self):
        for value in (5, 5.0):
            canned = {
                "row_count": 0,
                "sample_rows": [],
                "confirmation_token": "t",
                "token_expires_at": "now",
            }
            with patch.object(
                purge_mcp_server, "preview_purge_candidates", return_value=canned
            ) as preview:
                self.call(
                    "preview_purge",
                    {"policy_name": "high_scores", "params": {"min_score": value}},
                )

            (sent,) = preview.call_args.args[1].values()
            self.assertIs(type(sent), type(value))

    def test_a_validation_error_reaches_the_client_as_a_tool_error(self):
        result = self.call(
            "preview_purge",
            {"policy_name": "high_scores", "params": {"min_score": 1000}},
        )

        self.assertTrue(result.is_error)


FIELD_NAMES = {"created_at", "due_on", "level", "score", "amount", "status", "id"}

LISTING_POLICIES = {
    "stale_failed": {
        "model": "tests.PolicyRecord",
        "description": "Failed rows past retention.",
        "select": Q(status="failed"),
        "exclude": Q(status="held"),
        "max_rows": 50,
        "params": [
            {
                "name": "older_than_seconds",
                "type": "age_seconds",
                "field": "created_at",
                "min": 60,
                "max": 3600,
            },
        ],
    },
    "low_scores": {
        "model": "tests.PolicyRecord",
        "description": "Rows by numeric thresholds.",
        "params": [
            {
                "name": "max_level",
                "type": "number",
                "field": "level",
                "op": "lte",
                "min": 0,
                "max": 10,
            },
            {
                "name": "max_score",
                "type": "number",
                "field": "score",
                "op": "lt",
                "min": 0.5,
                "max": 9.5,
            },
        ],
    },
    "by_state": {
        "model": "tests.PolicyRecord",
        "description": "Rows in a given state.",
        "params": [
            {
                "name": "state",
                "type": "choice",
                "field": "status",
                "choices": ["failed", "cancelled"],
            },
        ],
    },
    "amounts": {
        "model": "tests.PolicyRecord",
        "description": "Rows by amount.",
        "params": [
            {
                "name": "max_amount",
                "type": "number",
                "field": "amount",
                "op": "lte",
                "min": 0,
                "max": 100,
            },
        ],
    },
}


def walk_strings(value):
    """Yield every dict key and string value in a nested structure."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from walk_strings(item)
    elif isinstance(value, str):
        yield value


class TestListPolicies(TestCase):
    def listing(self, policies=LISTING_POLICIES, **extra_settings):
        with override_settings(DB_PURGE_MCP_POLICIES=policies, **extra_settings):
            return {info["name"]: info for info in describe_policies()}

    def test_unset_or_empty_policies_list_nothing(self):
        with override_settings(DB_PURGE_MCP_POLICIES={}):
            self.assertEqual(describe_policies(), [])
        with override_settings():
            del settings.DB_PURGE_MCP_POLICIES
            self.assertEqual(describe_policies(), [])

    def test_describes_each_policy(self):
        info = self.listing()["stale_failed"]

        self.assertEqual(info["description"], "Failed rows past retention.")
        self.assertEqual(info["model"], "tests.PolicyRecord")
        self.assertEqual(info["action"], "delete")

    def test_max_rows_is_the_effective_cap(self):
        listing = self.listing(DB_PURGE_MCP_MAX_ROWS=200)

        self.assertEqual(listing["stale_failed"]["max_rows"], 50)
        self.assertEqual(listing["by_state"]["max_rows"], 200)
        self.assertEqual(self.listing()["by_state"]["max_rows"], 10000)

    def test_age_seconds_param_shows_op_and_bounds(self):
        (param,) = self.listing()["stale_failed"]["params"]

        self.assertEqual(
            param,
            {
                "name": "older_than_seconds",
                "type": "age_seconds",
                "op": "lte",
                "min": 60,
                "max": 3600,
            },
        )

    def test_number_param_says_whether_it_is_integer_only(self):
        level, score = self.listing()["low_scores"]["params"]
        (amount,) = self.listing()["amounts"]["params"]

        self.assertEqual(level["type"], "number")
        self.assertEqual((level["op"], level["min"], level["max"]), ("lte", 0, 10))
        self.assertIs(level["integer_only"], True)
        self.assertEqual((score["op"], score["min"], score["max"]), ("lt", 0.5, 9.5))
        self.assertIs(score["integer_only"], False)
        self.assertIs(amount["integer_only"], False)

    def test_choice_param_lists_its_choices(self):
        (param,) = self.listing()["by_state"]["params"]

        self.assertEqual(
            param,
            {"name": "state", "type": "choice", "choices": ["failed", "cancelled"]},
        )

    def test_int_choices_are_listed_as_ints(self):
        policies = {
            "by_level": {
                "model": "tests.PolicyRecord",
                "description": "d",
                "params": [
                    {
                        "name": "lvl",
                        "type": "choice",
                        "field": "level",
                        "choices": [1, 2],
                    }
                ],
            }
        }

        (param,) = self.listing(policies)["by_level"]["params"]

        self.assertEqual(param["choices"], [1, 2])

    def test_never_exposes_select_exclude_or_field_names(self):
        listing = describe_policies_json(LISTING_POLICIES)

        strings = set(walk_strings(listing))
        self.assertFalse(strings & FIELD_NAMES, strings & FIELD_NAMES)
        self.assertNotIn("select", strings)
        self.assertNotIn("exclude", strings)
        self.assertNotIn("field", strings)
        self.assertNotIn("Q(", json.dumps(listing))

    def test_is_read_only(self):
        self.listing()

        self.assertEqual(PolicyRecord.objects.count(), 0)

    def test_invalid_config_gives_an_opaque_error(self):
        bad = {"p": {**LISTING_POLICIES["by_state"], "action": "archive"}}
        with (
            override_settings(DB_PURGE_MCP_POLICIES=bad),
            self.assertLogs("dbpurge.policies", "ERROR"),
            self.assertRaises(PurgePolicyError) as ctx,
        ):
            describe_policies()

        self.assertNotIn("archive", str(ctx.exception))


def describe_policies_json(policies):
    """Round-trip the listing through JSON, as the wire would."""
    with override_settings(DB_PURGE_MCP_POLICIES=policies):
        return json.loads(json.dumps(describe_policies()))
