import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import connection
from django.db.models import Q, QuerySet
from django.test import TestCase, override_settings
from django.utils import timezone

from dbpurge.management.commands.purge_mcp_server import (
    _TOKENS,
    MISSING_FASTMCP_ERROR,
    TOKEN_ERROR_MESSAGE,
    PurgePolicyError,
    PurgeRowSetChangedError,
    describe_policies,
    execute_purge_candidates,
    get_purge_candidates,
    preview_purge_candidates,
    token_is_valid,
)
from tests.models import PolicyRecord, SampleRecord


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
        self.assertEqual(
            preview["sample_rows"],
            [
                {
                    "pk": self.old_record.pk,
                    "created_at": self.old_record.created_at.isoformat(),
                }
            ],
        )
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
        with (
            override_settings(DB_PURGE_MCP_ALLOWED_MODELS=[]),
            self.assertRaises(PurgePolicyError),
        ):
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
        with (
            override_settings(DB_PURGE_MCP_ALLOWED_MODELS=[]),
            self.assertRaises(PurgePolicyError),
        ):
            execute_purge_candidates(*self.params, self.token)

        self.assertEqual(SampleRecord.objects.count(), 1)

    def test_cap_breach_blocks_delete_and_leaves_token_usable(self):
        with (
            override_settings(DB_PURGE_MCP_MAX_ROWS=0),
            self.assertRaises(PurgePolicyError),
        ):
            execute_purge_candidates(*self.params, self.token)

        self.assertEqual(SampleRecord.objects.count(), 1)
        self.assertTrue(token_is_valid(self.token, *self.params))

    def test_retry_succeeds_after_a_cap_breach_is_resolved(self):
        with (
            override_settings(DB_PURGE_MCP_MAX_ROWS=0),
            self.assertRaises(PurgePolicyError),
        ):
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


class TestRowSetFingerprinting(TestCase):
    def setUp(self):
        self.old_record = SampleRecord.objects.create(
            created_at=timezone.now() - timedelta(days=10), label="old"
        )
        self.params = ("tests", "samplerecord", "created_at", 3600)

    def test_fingerprint_matches_execute_succeeds(self):
        preview = preview_purge_candidates(*self.params)

        result = execute_purge_candidates(*self.params, preview["confirmation_token"])

        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(SampleRecord.objects.count(), 0)

    def test_row_inserted_into_match_set_rejects_execute(self):
        preview = preview_purge_candidates(*self.params)
        token = preview["confirmation_token"]

        SampleRecord.objects.create(
            created_at=timezone.now() - timedelta(days=11), label="phantom"
        )

        with self.assertRaises(PurgeRowSetChangedError):
            execute_purge_candidates(*self.params, token)

        self.assertEqual(SampleRecord.objects.count(), 2)

    def test_row_removed_from_match_set_rejects_execute(self):
        preview = preview_purge_candidates(*self.params)
        token = preview["confirmation_token"]

        self.old_record.delete()

        with self.assertRaises(PurgeRowSetChangedError):
            execute_purge_candidates(*self.params, token)

    def test_row_mutated_out_of_filter_rejects_execute(self):
        preview = preview_purge_candidates(*self.params)
        token = preview["confirmation_token"]

        self.old_record.created_at = timezone.now()
        self.old_record.save()

        with self.assertRaises(PurgeRowSetChangedError):
            execute_purge_candidates(*self.params, token)

        self.assertEqual(SampleRecord.objects.count(), 1)

    def test_row_mutated_but_still_matching_pk_only_default_execute_succeeds(self):
        preview = preview_purge_candidates(*self.params)
        token = preview["confirmation_token"]

        self.old_record.label = "renamed"
        self.old_record.save()

        result = execute_purge_candidates(*self.params, token)

        self.assertEqual(result["deleted_count"], 1)

    def test_row_mutated_with_fingerprint_fields_configured_rejects_execute(self):
        with override_settings(
            DB_PURGE_MCP_FINGERPRINT_FIELDS={"tests.SampleRecord": ["label"]}
        ):
            preview = preview_purge_candidates(*self.params)
            token = preview["confirmation_token"]

            self.old_record.label = "renamed"
            self.old_record.save()

            with self.assertRaises(PurgeRowSetChangedError):
                execute_purge_candidates(*self.params, token)

    def test_unrelated_writes_do_not_affect_the_fingerprint(self):
        preview = preview_purge_candidates(*self.params)
        token = preview["confirmation_token"]

        # A write to a row that does not, and never did, match the
        # retention filter must not perturb the fingerprint.
        SampleRecord.objects.create(created_at=timezone.now(), label="unrelated")

        result = execute_purge_candidates(*self.params, token)

        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(SampleRecord.objects.count(), 1)

    def test_fingerprint_mismatch_reinstates_the_token(self):
        # Corrected design decision: drift is not caller error, so it
        # follows the same reinstate-on-failure path as a cap breach
        # rather than permanently burning the token.
        preview = preview_purge_candidates(*self.params)
        token = preview["confirmation_token"]

        self.old_record.delete()

        with self.assertRaises(PurgeRowSetChangedError):
            execute_purge_candidates(*self.params, token)

        self.assertTrue(token_is_valid(token, *self.params))

    def test_execute_succeeds_on_backend_without_select_for_update_support(self):
        # Proves execute still works when the backend can't lock rows
        # (true of SQLite here); it does NOT exercise the gate itself,
        # since has_select_for_update is False so select_for_update()
        # is never called. See test_select_for_update_applied_when_
        # backend_supports_it for that.
        self.assertFalse(connection.features.has_select_for_update)

        preview = preview_purge_candidates(*self.params)

        result = execute_purge_candidates(*self.params, preview["confirmation_token"])

        self.assertEqual(result["deleted_count"], 1)

    def test_select_for_update_applied_when_backend_supports_it(self):
        # SQLite itself can't run SELECT ... FOR UPDATE, so
        # select_for_update() is mocked at the queryset level (as a
        # no-op passthrough) rather than actually exercised against
        # the database; only the has_select_for_update gate and the
        # call are under test here.
        preview = preview_purge_candidates(*self.params)
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
            result = execute_purge_candidates(*self.params, token)

        mock_select_for_update.assert_called_once()
        self.assertEqual(result["deleted_count"], 1)


class TestMissingFastMCP(TestCase):
    @patch("dbpurge.management.commands.purge_mcp_server.FastMCP", None)
    def test_handle_raises_a_command_error_pointing_at_the_extra(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("purge_mcp_server")

        self.assertEqual(str(ctx.exception), MISSING_FASTMCP_ERROR)


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
