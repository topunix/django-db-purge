from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.test import SimpleTestCase, override_settings

from dbpurge.policies import load_policies

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
