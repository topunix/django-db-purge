import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import Q
from django.test import SimpleTestCase, TestCase, override_settings

from dbpurge.policies import (
    PurgePolicyError,
    build_queryset,
    cutoff_bound,
    get_policy,
    hash_params,
    load_policies,
    validate_request,
)
from tests.models import PolicyNote, PolicyRecord

AGE = {
    "name": "older_than_seconds",
    "type": "age_seconds",
    "field": "created_at",
    "min": 60,
    "max": 3600,
}
NUMBER = {
    "name": "min_score",
    "type": "number",
    "field": "score",
    "op": "gte",
    "min": 0,
    "max": 100,
}
CHOICE = {
    "name": "status",
    "type": "choice",
    "field": "status",
    "choices": ["failed", "cancelled"],
}


def policy(**overrides):
    base = {
        "model": "tests.PolicyRecord",
        "description": "A test policy.",
        "select": Q(status="failed"),
    }
    base.update(overrides)
    return base


class PolicyConfigTestCase(SimpleTestCase):
    def load(self, policies, **extra_settings):
        with override_settings(DB_PURGE_MCP_POLICIES=policies, **extra_settings):
            return load_policies()

    def assertInvalid(self, raw_policy, **extra_settings):
        with self.assertRaises(ImproperlyConfigured):
            self.load({"p": raw_policy}, **extra_settings)

    def assertInvalidEach(self, cases, make):
        for case in cases:
            with self.subTest(case=case):
                self.assertInvalid(make(case))


class TestLoadPolicies(PolicyConfigTestCase):
    def test_unset_setting_loads_nothing(self):
        with override_settings(DB_PURGE_MCP_POLICIES={}):
            del settings.DB_PURGE_MCP_POLICIES
            self.assertEqual(load_policies(), {})

    def test_empty_setting_loads_nothing(self):
        self.assertEqual(self.load({}), {})

    def test_setting_must_be_a_dict(self):
        for value in ([], "x", 3, ["a"]):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                self.load(value)

    def test_a_full_policy_loads(self):
        loaded = self.load(
            {
                "p": policy(
                    action="delete",
                    exclude=Q(status="held"),
                    params=[AGE, NUMBER],
                    max_rows=50,
                )
            }
        )
        loaded_policy = loaded["p"]
        self.assertEqual(loaded_policy.max_rows, 50)
        self.assertEqual(
            [p.name for p in loaded_policy.params], ["older_than_seconds", "min_score"]
        )
        self.assertEqual(loaded_policy.params[0].op, "lte")

    def test_policy_names_must_match_the_pattern(self):
        for name in ("Bad", "1bad", "has-dash", "", "a" * 65, "new\nline"):
            with self.subTest(name=name), self.assertRaises(ImproperlyConfigured):
                self.load({name: policy()})
        self.load({"a" * 64: policy()})

    def test_a_trailing_newline_is_refused_in_names(self):
        # "$" alone matches before a trailing newline, so fullmatch is needed.
        with self.assertRaises(ImproperlyConfigured):
            self.load({"ok\n": policy()})
        self.assertInvalid(policy(params=[{**AGE, "name": "ok\n"}]))

    def test_unknown_top_level_key_rejected(self):
        self.assertInvalid(policy(unexpected=1))

    def test_unknown_param_key_rejected(self):
        self.assertInvalid(policy(params=[{**AGE, "extra": 1}]))

    def test_keys_that_do_not_apply_to_a_param_type_rejected(self):
        self.assertInvalid(policy(params=[{**AGE, "choices": ["x"]}]))
        self.assertInvalid(policy(params=[{**CHOICE, "min": 1}]))

    def test_param_missing_a_required_key_rejected(self):
        for key in ("name", "type", "field", "min", "max"):
            with self.subTest(key=key):
                self.assertInvalid(
                    policy(params=[{k: v for k, v in AGE.items() if k != key}])
                )
        self.assertInvalid(
            policy(params=[{k: v for k, v in NUMBER.items() if k != "op"}])
        )

    def test_model_and_description_are_required(self):
        for key in ("model", "description"):
            raw = policy()
            del raw[key]
            with self.subTest(key=key):
                self.assertInvalid(raw)

    def test_description_must_be_a_non_empty_string(self):
        self.assertInvalidEach(["", "  ", 3, None], lambda v: policy(description=v))

    def test_model_must_resolve(self):
        self.assertInvalidEach(
            ["tests.Nope", "nope.Model", "PolicyRecord", "", 3, None],
            lambda v: policy(model=v),
        )

    def test_model_must_be_in_the_allowlist(self):
        self.assertInvalid(policy(), DB_PURGE_MCP_ALLOWED_MODELS=[])
        self.assertInvalid(policy(), DB_PURGE_MCP_ALLOWED_MODELS=["tests.SampleRecord"])

    def test_allowlist_matches_regardless_of_casing(self):
        self.load({"p": policy()}, DB_PURGE_MCP_ALLOWED_MODELS=["tests.policyrecord"])

    def test_action_must_be_delete(self):
        self.assertInvalidEach(
            ["archive", "DELETE", None, 3], lambda v: policy(action=v)
        )

    def test_action_defaults_to_delete(self):
        self.assertEqual(self.load({"p": policy()})["p"].action, "delete")

    def test_a_policy_needs_select_or_a_param(self):
        raw = policy()
        del raw["select"]
        self.assertInvalid(raw)
        self.assertInvalid({**raw, "exclude": Q(status="held")})
        self.load({"p": {**raw, "params": [AGE]}})

    def test_select_and_exclude_must_be_q_objects(self):
        self.assertInvalidEach(
            [{"status": "x"}, "x", None, [Q()]], lambda v: policy(select=v)
        )
        self.assertInvalidEach(
            [{"status": "x"}, "x", None], lambda v: policy(exclude=v)
        )

    def test_empty_q_rejected_for_select_and_exclude(self):
        empties = [Q(), ~Q(), Q(Q()), Q() & Q(), Q() | Q()]
        for empty in empties:
            with self.subTest(select=empty):
                self.assertInvalid(policy(select=empty))
            with self.subTest(exclude=empty):
                self.assertInvalid(policy(exclude=empty))

    def test_empty_select_does_not_count_as_a_select(self):
        raw = policy(select=Q())
        self.assertInvalid({**raw, "params": [AGE]})
        self.load({"p": policy(select=~Q(status="held"))})
        self.load({"p": policy(exclude=~Q(status="held"), params=[AGE])})

    def test_select_naming_a_missing_field_fails_at_load(self):
        self.assertInvalid(policy(select=Q(no_such_field=1)))
        self.assertInvalid(policy(exclude=Q(no_such_field=1)))

    def test_select_may_join_a_to_many_relation(self):
        self.load({"p": policy(select=Q(notes__body="x"))})

    def test_params_must_be_a_list_of_at_most_two(self):
        self.assertInvalid(policy(params=(AGE,)))
        self.assertInvalid(policy(params=[AGE, NUMBER, CHOICE]))
        self.assertInvalid(policy(params=[AGE, "x"]))

    def test_param_field_must_exist_on_the_model(self):
        self.assertInvalid(policy(params=[{**AGE, "field": "missing"}]))

    def test_param_field_must_not_contain_double_underscore(self):
        self.assertInvalid(policy(params=[{**AGE, "field": "created_at__date"}]))

    def test_param_field_must_not_be_the_pk_or_relational(self):
        number = {**NUMBER, "min": 0, "max": 10}
        self.assertInvalid(policy(params=[{**number, "field": "id"}]))
        self.assertInvalid(policy(params=[{**number, "field": "pk"}]))
        raw = {"model": "tests.PolicyNote", "description": "d"}
        self.assertInvalid({**raw, "params": [{**number, "field": "record"}]})
        self.assertInvalid({**raw, "params": [{**number, "field": "record_id"}]})
        self.assertInvalid(policy(params=[{**number, "field": "notes"}]))

    def test_param_names_must_be_unique_and_well_formed(self):
        self.assertInvalid(policy(params=[AGE, {**NUMBER, "name": AGE["name"]}]))
        self.assertInvalidEach(
            ["Bad", "", "a-b", 3], lambda v: policy(params=[{**AGE, "name": v}])
        )

    def test_same_field_pair_must_be_the_same_range_type(self):
        number_on_level = {**NUMBER, "field": "level"}
        self.assertInvalid(
            policy(
                params=[
                    {**NUMBER, "field": "score"},
                    {
                        **number_on_level,
                        "field": "score",
                        "name": "b",
                        "type": "age_seconds",
                    },
                ]
            )
        )

    def test_same_field_pair_needs_opposite_ops(self):
        low = {**NUMBER, "name": "low", "op": "gte"}
        for op in ("gte", "gt"):
            with self.subTest(op=op):
                self.assertInvalid(
                    policy(params=[low, {**low, "name": "high", "op": op}])
                )
        for op in ("lt", "lte"):
            with self.subTest(op=op):
                self.load(
                    {"p": policy(params=[low, {**low, "name": "high", "op": op}])}
                )
        older = {**AGE, "name": "older"}
        self.assertInvalid(policy(params=[older, {**AGE, "name": "also_older"}]))
        self.load({"p": policy(params=[older, {**AGE, "name": "newer", "op": "gte"}])})

    def test_two_choice_params_on_one_field_rejected(self):
        self.assertInvalid(policy(params=[CHOICE, {**CHOICE, "name": "status_b"}]))

    def test_max_rows_must_be_a_positive_int_within_the_global_cap(self):
        self.assertInvalidEach(
            [0, -1, True, 1.5, "5", None], lambda v: policy(max_rows=v)
        )
        self.assertInvalid(policy(max_rows=11), DB_PURGE_MCP_MAX_ROWS=10)
        self.load({"p": policy(max_rows=10)}, DB_PURGE_MCP_MAX_ROWS=10)

    def test_max_rows_checked_against_the_default_cap_when_unset(self):
        self.load({"p": policy(max_rows=10000)})
        self.assertInvalid(policy(max_rows=10001))


class TestParamConfig(PolicyConfigTestCase):
    def test_age_seconds_bounds(self):
        self.assertInvalidEach(
            [
                (0, 10),
                (-5, 10),
                (10, 5),
                (1.5, 10),
                (1, 10.5),
                (True, 10),
                (1, True),
                ("1", 10),
            ],
            lambda b: policy(params=[{**AGE, "min": b[0], "max": b[1]}]),
        )
        self.load({"p": policy(params=[{**AGE, "min": 5, "max": 5}])})

    def test_age_seconds_max_must_be_representable(self):
        for maximum in (10**12, 2**62, 10**30):
            with self.subTest(maximum=maximum):
                self.assertInvalid(policy(params=[{**AGE, "max": maximum}]))

    def test_age_seconds_op_and_field_type(self):
        self.assertInvalidEach(
            ["lt", "gt", "eq", None], lambda v: policy(params=[{**AGE, "op": v}])
        )
        self.assertInvalid(policy(params=[{**AGE, "field": "score"}]))
        self.load({"p": policy(params=[{**AGE, "field": "due_on", "op": "gte"}])})

    def test_number_requires_a_valid_op(self):
        self.assertInvalidEach(
            ["eq", "", None, 3], lambda v: policy(params=[{**NUMBER, "op": v}])
        )
        for op in ("lt", "lte", "gt", "gte"):
            self.load({"p": policy(params=[{**NUMBER, "op": op}])})

    def test_number_bounds(self):
        nan, inf = float("nan"), float("inf")
        self.assertInvalidEach(
            [
                (5, 1),
                (True, 5),
                (1, True),
                (nan, 5),
                (1, nan),
                (-inf, 5),
                (1, inf),
                ("1", 5),
            ],
            lambda b: policy(params=[{**NUMBER, "min": b[0], "max": b[1]}]),
        )
        self.load({"p": policy(params=[{**NUMBER, "min": 0.5, "max": 0.5}])})

    def test_number_field_must_be_numeric(self):
        self.assertInvalid(policy(params=[{**NUMBER, "field": "status"}]))
        self.assertInvalid(policy(params=[{**NUMBER, "field": "created_at"}]))
        for field in ("level", "score", "amount"):
            with self.subTest(field=field):
                self.load({"p": policy(params=[{**NUMBER, "field": field}])})

    def test_choices_shape(self):
        self.assertInvalidEach(
            [
                [],
                "ab",
                None,
                ["a", "a"],
                [1, 1],
                ["a", 1],
                [True, False],
                [1.5],
                [None],
                [["a"]],
            ],
            lambda v: policy(params=[{**CHOICE, "choices": v}]),
        )

    def test_choices_must_match_the_field_type(self):
        self.assertInvalid(policy(params=[{**CHOICE, "field": "level"}]))
        self.assertInvalid(policy(params=[{**CHOICE, "choices": [1, 2]}]))
        self.load(
            {"p": policy(params=[{**CHOICE, "field": "level", "choices": [1, 2]}])}
        )
        self.assertInvalid(policy(params=[{**CHOICE, "field": "created_at"}]))


NOW = datetime(2024, 6, 10, 12, 0, tzinfo=timezone.utc)


class RequestTestCase(SimpleTestCase):
    def policy_with(self, *params, **overrides):
        raw = policy(params=list(params), **overrides)
        with override_settings(DB_PURGE_MCP_POLICIES={"p": raw}):
            return get_policy("p")

    def assertRejected(self, loaded, params):
        with self.assertRaises(PurgePolicyError) as ctx:
            validate_request(loaded, params)
        return str(ctx.exception)


class TestGetPolicy(SimpleTestCase):
    def test_unknown_policy_rejected_without_echoing_the_name(self):
        with override_settings(DB_PURGE_MCP_POLICIES={"p": policy()}):
            for name in ("nope", "evil\nINJECTED line", None, 3, ["p"]):
                with (
                    self.subTest(name=name),
                    self.assertRaises(PurgePolicyError) as ctx,
                ):
                    get_policy(name)
                self.assertNotIn("INJECTED", str(ctx.exception))
                self.assertNotIn("nope", str(ctx.exception))

    def test_no_policies_means_every_name_is_rejected(self):
        with (
            override_settings(DB_PURGE_MCP_POLICIES={}),
            self.assertRaises(PurgePolicyError),
        ):
            get_policy("p")

    def test_invalid_config_gives_an_opaque_error_and_a_server_log(self):
        bad = {"p": policy(action="archive")}
        with (
            override_settings(DB_PURGE_MCP_POLICIES=bad),
            self.assertLogs("dbpurge.policies", "ERROR") as logs,
            self.assertRaises(PurgePolicyError) as ctx,
        ):
            get_policy("p")
        self.assertNotIn("action", str(ctx.exception))
        self.assertIn("action must be", "".join(logs.output))

    def test_config_is_reloaded_on_every_call(self):
        with override_settings(DB_PURGE_MCP_POLICIES={"p": policy()}):
            get_policy("p")
        with (
            override_settings(DB_PURGE_MCP_POLICIES={}),
            self.assertRaises(PurgePolicyError),
        ):
            get_policy("p")

    def test_allowlist_is_rechecked_on_every_call(self):
        with override_settings(DB_PURGE_MCP_POLICIES={"p": policy()}):
            get_policy("p")
            with (
                override_settings(DB_PURGE_MCP_ALLOWED_MODELS=[]),
                self.assertRaises(PurgePolicyError),
            ):
                get_policy("p")


class TestValidateRequest(RequestTestCase):
    def test_none_and_empty_params_are_the_same_for_a_param_less_policy(self):
        loaded = self.policy_with()
        self.assertEqual(validate_request(loaded, None), {})
        self.assertEqual(validate_request(loaded, {}), {})

    def test_keys_must_equal_the_declared_names(self):
        loaded = self.policy_with(AGE, NUMBER)
        both = {"older_than_seconds": 100, "min_score": 5}
        self.assertEqual(validate_request(loaded, both), both)
        for params in (
            None,
            {},
            {"older_than_seconds": 100},
            {**both, "extra": 1},
            {"older_than_seconds": 100, "other": 5},
        ):
            with self.subTest(params=params):
                self.assertRejected(loaded, params)

    def test_extra_keys_are_not_echoed(self):
        loaded = self.policy_with()
        message = self.assertRejected(loaded, {"evil\nINJECTED": 1})
        self.assertNotIn("INJECTED", message)

    def test_params_must_be_an_object(self):
        loaded = self.policy_with(AGE)
        for params in ([1], "x", 5):
            with self.subTest(params=params):
                self.assertRejected(loaded, params)

    def test_age_seconds_accepts_only_in_bounds_ints(self):
        loaded = self.policy_with(AGE)
        for value in (60, 3600, 1000):
            self.assertEqual(
                validate_request(loaded, {"older_than_seconds": value}),
                {"older_than_seconds": value},
            )
        for value in (59, 3601, -1, 0, 100.0, 100.5, True, "100", None, [100]):
            with self.subTest(value=value):
                self.assertRejected(loaded, {"older_than_seconds": value})

    def test_number_accepts_in_bounds_ints_and_floats(self):
        loaded = self.policy_with(NUMBER)
        for value in (0, 100, 0.5, 99.99, 5):
            self.assertEqual(
                validate_request(loaded, {"min_score": value}), {"min_score": value}
            )
        for value in (-0.1, 100.1, True, False, "5", None, {}):
            with self.subTest(value=value):
                self.assertRejected(loaded, {"min_score": value})

    def test_number_rejects_nan_and_infinity(self):
        loaded = self.policy_with(NUMBER)
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertRejected(loaded, {"min_score": value})

    def test_choice_requires_an_exact_declared_value(self):
        loaded = self.policy_with(CHOICE)
        self.assertEqual(
            validate_request(loaded, {"status": "failed"}), {"status": "failed"}
        )
        for value in ("other", "FAILED", " failed", 1, True, None, ["failed"]):
            with self.subTest(value=value):
                self.assertRejected(loaded, {"status": value})

    def test_choice_is_type_strict(self):
        int_choice = {**CHOICE, "field": "level", "choices": [1, 2]}
        loaded = self.policy_with(int_choice)
        self.assertEqual(validate_request(loaded, {"status": 1}), {"status": 1})
        for value in (True, 1.0, "1", 3):
            with self.subTest(value=value):
                self.assertRejected(loaded, {"status": value})

    def test_error_messages_never_echo_supplied_strings(self):
        loaded = self.policy_with(CHOICE)
        message = self.assertRejected(loaded, {"status": "evil\nINJECTED"})
        self.assertNotIn("INJECTED", message)


class TestWindowValidation(RequestTestCase):
    older = {**AGE, "name": "older_than", "min": 1, "max": 1000}
    newer = {**AGE, "name": "newer_than", "min": 1, "max": 1000, "op": "gte"}

    def test_age_window_valid_when_newer_offset_is_larger(self):
        loaded = self.policy_with(self.older, self.newer)
        params = {"older_than": 100, "newer_than": 500}
        self.assertEqual(validate_request(loaded, params), params)

    def test_age_window_inverted_rejected(self):
        loaded = self.policy_with(self.older, self.newer)
        self.assertRejected(loaded, {"older_than": 500, "newer_than": 100})

    def test_age_window_with_equal_bounds_is_a_valid_point(self):
        loaded = self.policy_with(self.older, self.newer)
        params = {"older_than": 100, "newer_than": 100}
        self.assertEqual(validate_request(loaded, params), params)

    def number_window(self, low_op, up_op):
        low = {**NUMBER, "name": "low", "op": low_op}
        high = {**NUMBER, "name": "high", "op": up_op}
        return self.policy_with(low, high)

    def test_number_window_valid_and_inverted(self):
        loaded = self.number_window("gte", "lte")
        self.assertEqual(
            validate_request(loaded, {"low": 1, "high": 2}), {"low": 1, "high": 2}
        )
        self.assertRejected(loaded, {"low": 3, "high": 2})

    def test_number_window_equal_bounds_only_valid_when_both_inclusive(self):
        equal = {"low": 5, "high": 5}
        self.assertEqual(
            validate_request(self.number_window("gte", "lte"), equal), equal
        )
        for low_op, up_op in (("gt", "lte"), ("gte", "lt"), ("gt", "lt")):
            with self.subTest(ops=(low_op, up_op)):
                self.assertRejected(self.number_window(low_op, up_op), equal)


class TestHashParams(SimpleTestCase):
    def test_matches_the_documented_canonical_form(self):
        raw = json.dumps(
            {"policy": "p", "params": {"a": 1, "b": "x"}},
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hashlib.sha256(raw.encode()).hexdigest()
        self.assertEqual(hash_params("p", {"a": 1, "b": "x"}), expected)

    def test_stable_across_param_key_order(self):
        self.assertEqual(
            hash_params("p", {"a": 1, "b": 2}), hash_params("p", {"b": 2, "a": 1})
        )

    def test_differs_by_policy_and_by_params(self):
        base = hash_params("p", {"a": 1})
        self.assertNotEqual(base, hash_params("q", {"a": 1}))
        self.assertNotEqual(base, hash_params("p", {"a": 2}))
        self.assertNotEqual(base, hash_params("p", {"b": 1}))

    def test_equal_int_and_float_hash_differently(self):
        self.assertNotEqual(hash_params("p", {"a": 5}), hash_params("p", {"a": 5.0}))

    def test_no_param_boundary_ambiguity(self):
        self.assertNotEqual(
            hash_params("p", {"a": "1|2"}), hash_params("p", {"a": "1", "b": "2"})
        )


class TestCutoffBound(SimpleTestCase):
    def test_datetime_field_keeps_the_aware_datetime(self):
        field = PolicyRecord._meta.get_field("created_at")
        self.assertEqual(cutoff_bound(field, NOW), NOW)

    def test_date_field_gets_a_date_not_a_datetime(self):
        field = PolicyRecord._meta.get_field("due_on")
        bound = cutoff_bound(field, NOW)
        self.assertIs(type(bound), date)

    @override_settings(TIME_ZONE="UTC")
    def test_date_field_uses_the_cutoff_calendar_date(self):
        field = PolicyRecord._meta.get_field("due_on")
        late = datetime(2024, 6, 8, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(cutoff_bound(field, late), date(2024, 6, 8))


def make_record(**kwargs):
    defaults = {
        "created_at": NOW - timedelta(days=1),
        "due_on": date(2024, 6, 9),
    }
    return PolicyRecord.objects.create(**{**defaults, **kwargs})


class BuildQuerysetTestCase(TestCase):
    def matched(self, *params, params_given=None, now=NOW, **overrides):
        overrides.setdefault("select", Q(pk__gt=0))
        raw = policy(params=list(params), **overrides)
        with override_settings(DB_PURGE_MCP_POLICIES={"p": raw}):
            loaded = get_policy("p")
        validated = validate_request(loaded, params_given)
        queryset = build_queryset(loaded, validated, now)
        return sorted(queryset.values_list("pk", flat=True))


class TestBuildQueryset(BuildQuerysetTestCase):
    def test_select_and_exclude_apply(self):
        keep = make_record(status="failed")
        make_record(status="new")
        make_record(status="held")
        pks = self.matched(
            select=Q(status__in=["failed", "held"]), exclude=Q(status="held")
        )
        self.assertEqual(pks, [keep.pk])

    def test_age_seconds_lte_and_gte(self):
        old = make_record(created_at=NOW - timedelta(days=10))
        fresh = make_record(created_at=NOW - timedelta(minutes=1))
        older = {**AGE, "min": 1, "max": 10**8}
        newer = {**older, "op": "gte"}
        self.assertEqual(
            self.matched(older, params_given={"older_than_seconds": 3600}),
            [old.pk],
        )
        self.assertEqual(
            self.matched(newer, params_given={"older_than_seconds": 3600}),
            [fresh.pk],
        )

    def test_age_boundary_is_inclusive(self):
        edge = make_record(created_at=NOW - timedelta(seconds=3600))
        older = {**AGE, "min": 1, "max": 10**8}
        pks = self.matched(older, params_given={"older_than_seconds": 3600})
        self.assertEqual(pks, [edge.pk])

    def test_date_field_age_compares_dates(self):
        make_record(due_on=date(2024, 6, 9))
        edge = make_record(due_on=date(2024, 6, 8))
        older_on_due = {**AGE, "field": "due_on", "min": 1, "max": 10**8}
        with override_settings(TIME_ZONE="UTC"):
            pks = self.matched(
                older_on_due, params_given={"older_than_seconds": 2 * 86400}
            )
        self.assertEqual(pks, [edge.pk])

    def test_number_ops(self):
        low = make_record(score=1.0)
        mid = make_record(score=5.0)
        high = make_record(score=9.0)
        cases = {
            "lt": [low.pk],
            "lte": [low.pk, mid.pk],
            "gt": [high.pk],
            "gte": [mid.pk, high.pk],
        }
        for op, expected in cases.items():
            number = {**NUMBER, "op": op, "min": 0, "max": 10}
            with self.subTest(op=op):
                pks = self.matched(number, params_given={"min_score": 5})
                self.assertEqual(pks, sorted(expected))

    def test_number_compares_decimal_fields_exactly(self):
        row = make_record(amount=Decimal("0.10"))
        make_record(amount=Decimal("0.11"))
        number = {**NUMBER, "field": "amount", "op": "lte", "min": 0, "max": 1}
        self.assertEqual(
            self.matched(number, params_given={"min_score": 0.1}), [row.pk]
        )

    def test_choice_filters_by_exact_match(self):
        failed = make_record(status="failed")
        make_record(status="cancelled")
        pks = self.matched(CHOICE, params_given={"status": "failed"})
        self.assertEqual(pks, [failed.pk])

    def test_window_selects_only_rows_inside_it(self):
        make_record(score=1.0)
        inside = make_record(score=5.0)
        make_record(score=9.0)
        low = {**NUMBER, "name": "low", "op": "gte"}
        high = {**NUMBER, "name": "high", "op": "lt"}
        pks = self.matched(low, high, params_given={"low": 3, "high": 7})
        self.assertEqual(pks, [inside.pk])


class TestToManyJoins(BuildQuerysetTestCase):
    def setUp(self):
        self.record = make_record()
        for _ in range(3):
            PolicyNote.objects.create(record=self.record, body="x")
        self.other = make_record()
        PolicyNote.objects.create(record=self.other, body="y")

    def queryset(self, **overrides):
        raw = policy(**overrides)
        with override_settings(DB_PURGE_MCP_POLICIES={"p": raw}):
            return build_queryset(get_policy("p"), {}, NOW)

    def test_select_joining_a_to_many_returns_each_row_once(self):
        queryset = self.queryset(select=Q(notes__body="x"))
        self.assertEqual(queryset.count(), 1)
        self.assertEqual(list(queryset), [self.record])

    def test_exclude_joining_a_to_many_returns_each_row_once(self):
        queryset = self.queryset(select=Q(pk__gt=0), exclude=Q(notes__body="x"))
        self.assertEqual(list(queryset), [self.other])

    def test_does_not_use_distinct_and_works_with_select_for_update(self):
        queryset = self.queryset(select=Q(notes__body__in=["x", "y"]))
        self.assertNotIn("DISTINCT", str(queryset.query))
        with transaction.atomic():
            locked = queryset.select_for_update()
            self.assertEqual(locked.count(), 2)
            self.assertEqual(len(list(locked)), 2)
