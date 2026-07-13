from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from dbpurge.management.commands.purge_mcp_server import (
    MISSING_FASTMCP_ERROR,
    TOKEN_ERROR_MESSAGE,
    PurgePolicyError,
    _TOKENS,
    execute_purge_candidates,
    get_purge_candidates,
    preview_purge_candidates,
    token_is_valid,
)
from tests.models import SampleRecord


def expire_token(token):
    _TOKENS[token] = replace(
        _TOKENS[token], expires_at=timezone.now() - timedelta(seconds=1)
    )


class TestListPurgeCandidates(TestCase):
    def test_includes_toy_model_date_columns(self):
        candidates = get_purge_candidates()

        self.assertIn("tests", candidates)
        self.assertEqual(candidates["tests"]["samplerecord"], ["created_at"])

    def test_read_only(self):
        SampleRecord.objects.create(created_at=timezone.now(), label="x")

        get_purge_candidates()

        self.assertEqual(SampleRecord.objects.count(), 1)


class TestPreviewPurgeCandidates(TestCase):
    def setUp(self):
        self.old_record = SampleRecord.objects.create(
            created_at=timezone.now() - timedelta(days=10), label="old"
        )
        self.params = ("tests", "samplerecord", "created_at", 3600)

    def test_issues_a_valid_token_bound_to_the_params(self):
        preview = preview_purge_candidates(*self.params)

        self.assertEqual(preview["row_count"], 1)
        self.assertEqual(preview["sample_rows"], [
            {"pk": self.old_record.pk, "created_at": self.old_record.created_at.isoformat()}
        ])
        self.assertTrue(token_is_valid(preview["confirmation_token"], *self.params))

    def test_caps_sample_rows_at_five(self):
        SampleRecord.objects.all().delete()
        for i in range(7):
            SampleRecord.objects.create(
                created_at=timezone.now() - timedelta(days=10, minutes=i), label=str(i)
            )

        preview = preview_purge_candidates(*self.params)

        self.assertEqual(preview["row_count"], 7)
        self.assertEqual(len(preview["sample_rows"]), 5)

    def test_performs_no_deletes(self):
        preview_purge_candidates(*self.params)

        self.assertEqual(SampleRecord.objects.count(), 1)

    def test_token_rejected_once_expired(self):
        preview = preview_purge_candidates(*self.params)
        token = preview["confirmation_token"]
        expire_token(token)

        self.assertFalse(token_is_valid(token, *self.params))

    def test_token_rejected_on_parameter_drift(self):
        preview = preview_purge_candidates(*self.params)
        token = preview["confirmation_token"]

        drifted_params = ("tests", "samplerecord", "created_at", 7200)

        self.assertFalse(token_is_valid(token, *drifted_params))

    def test_unknown_token_rejected(self):
        self.assertFalse(token_is_valid("not-a-real-token", *self.params))

    def test_denied_for_a_model_outside_the_allowlist(self):
        with override_settings(DB_PURGE_MCP_ALLOWED_MODELS=[]):
            with self.assertRaises(PurgePolicyError):
                preview_purge_candidates(*self.params)

    def test_allowlist_matches_regardless_of_casing(self):
        # An operator naturally reaches for "tests.samplerecord", the
        # lowercase form list_purge_candidates itself shows them, not
        # the class-cased "tests.SampleRecord" label. Either must work.
        with override_settings(DB_PURGE_MCP_ALLOWED_MODELS=["tests.samplerecord"]):
            preview_purge_candidates(*self.params)


class TestExecutePurgeCandidates(TestCase):
    def setUp(self):
        self.old_record = SampleRecord.objects.create(
            created_at=timezone.now() - timedelta(days=10), label="old"
        )
        self.params = ("tests", "samplerecord", "created_at", 3600)
        preview = preview_purge_candidates(*self.params)
        self.token = preview["confirmation_token"]

    def _error_message_for(self, token, params):
        with self.assertRaises(PurgePolicyError) as ctx:
            execute_purge_candidates(*params, token)
        return str(ctx.exception)

    def test_deletes_matching_rows_and_returns_the_real_total(self):
        result = execute_purge_candidates(*self.params, self.token)

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
        execute_purge_candidates(*self.params, self.token)
        self.assertNotIn(self.token, _TOKENS)

        message = self._error_message_for(self.token, self.params)

        self.assertEqual(message, TOKEN_ERROR_MESSAGE)

    def test_denied_for_a_model_outside_the_allowlist(self):
        with override_settings(DB_PURGE_MCP_ALLOWED_MODELS=[]):
            with self.assertRaises(PurgePolicyError):
                execute_purge_candidates(*self.params, self.token)

        self.assertEqual(SampleRecord.objects.count(), 1)

    def test_cap_breach_blocks_delete_and_leaves_token_usable(self):
        with override_settings(DB_PURGE_MCP_MAX_ROWS=0):
            with self.assertRaises(PurgePolicyError):
                execute_purge_candidates(*self.params, self.token)

        self.assertEqual(SampleRecord.objects.count(), 1)
        self.assertTrue(token_is_valid(self.token, *self.params))

    def test_retry_succeeds_after_a_cap_breach_is_resolved(self):
        with override_settings(DB_PURGE_MCP_MAX_ROWS=0):
            with self.assertRaises(PurgePolicyError):
                execute_purge_candidates(*self.params, self.token)

        result = execute_purge_candidates(*self.params, self.token)

        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(SampleRecord.objects.count(), 0)

    def test_allowlist_matches_regardless_of_casing(self):
        with override_settings(DB_PURGE_MCP_ALLOWED_MODELS=["tests.samplerecord"]):
            result = execute_purge_candidates(*self.params, self.token)

        self.assertEqual(result["deleted_count"], 1)

    def test_opaque_message_identical_for_unknown_expired_and_drifted_tokens(self):
        unknown_message = self._error_message_for("not-a-real-token", self.params)

        expire_token(self.token)
        expired_message = self._error_message_for(self.token, self.params)

        fresh_preview = preview_purge_candidates(*self.params)
        drifted_params = ("tests", "samplerecord", "created_at", 7200)
        drifted_message = self._error_message_for(
            fresh_preview["confirmation_token"], drifted_params
        )

        self.assertEqual(unknown_message, TOKEN_ERROR_MESSAGE)
        self.assertEqual(expired_message, TOKEN_ERROR_MESSAGE)
        self.assertEqual(drifted_message, TOKEN_ERROR_MESSAGE)


class TestMissingFastMCP(TestCase):
    @patch("dbpurge.management.commands.purge_mcp_server.FastMCP", None)
    def test_handle_raises_a_command_error_pointing_at_the_extra(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("purge_mcp_server")

        self.assertEqual(str(ctx.exception), MISSING_FASTMCP_ERROR)
