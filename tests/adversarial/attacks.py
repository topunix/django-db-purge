"""
Attack implementations, one function per case id in registry.py.

Each attack runs against the real tool functions, or against the tool
schema through an in-memory fastmcp.Client for protocol cases, and
returns an Outcome: "blocked" when the server stopped the attack and no
row was deleted that should not have been, "not_blocked" otherwise, plus
a short evidence string. Evidence is deterministic on purpose: no
tokens, timestamps, durations or primary keys.

Harness-only policies and allowlists are applied with override_settings,
on the toy models in tests/models.py. run_attack clears the token store
before each case so cases cannot affect one another.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import patch

import fastmcp
from django.test import override_settings
from django.utils import timezone

from dbpurge.management.commands import purge_mcp_server
from dbpurge.management.commands.purge_mcp_server import (
    _TOKENS,
    TOKEN_ERROR_MESSAGE,
    TOKEN_TTL,
    PurgePolicyError,
    PurgeRowCapError,
    PurgeRowSetChangedError,
    describe_policies,
    execute_purge_candidates,
    preview_purge_candidates,
)
from tests.models import PolicyRecord, SampleRecord

PAYLOADS: dict[str, list[str]] = json.loads(
    (Path(__file__).parent / "payloads.json").read_text(encoding="utf-8")
)

OVERSIZED_LENGTH = 1024 * 1024
LOG_RECORD_LIMIT = 4096


class Outcome(NamedTuple):
    observed: str
    evidence: str


def blocked(evidence: str) -> Outcome:
    return Outcome("blocked", evidence)


def not_blocked(evidence: str) -> Outcome:
    return Outcome("not_blocked", evidence)


ATTACKS: dict[str, Callable[[], Outcome]] = {}


def attack(case_id: str) -> Callable[[Callable[[], Outcome]], Callable[[], Outcome]]:
    """Register fn as the attack for case_id."""

    def register(fn: Callable[[], Outcome]) -> Callable[[], Outcome]:
        ATTACKS[case_id] = fn
        return fn

    return register


def run_attack(case_id: str) -> Outcome:
    """Run one attack with a clean token store before and after."""
    _TOKENS.clear()
    try:
        return ATTACKS[case_id]()
    finally:
        _TOKENS.clear()


# Harness policies. Every case that needs policies builds them from these.

STALE = "stale_samples"
OTHER = "other_samples"
LEVEL = "low_level"
SCORE = "low_score"
STATUS = "by_status"
WINDOW = "window_samples"

SAMPLE_AGE = {
    "name": "older_than_seconds",
    "type": "age_seconds",
    "field": "created_at",
    "min": 1,
    "max": 315360000,
}


def _policies(**extra: dict[str, Any]) -> dict[str, dict[str, Any]]:
    base: dict[str, dict[str, Any]] = {
        STALE: {
            "model": "tests.SampleRecord",
            "description": "Sample records older than the given age.",
            "params": [SAMPLE_AGE],
        },
        OTHER: {
            "model": "tests.SampleRecord",
            "description": "A second policy over the same model.",
            "params": [SAMPLE_AGE],
        },
        LEVEL: {
            "model": "tests.PolicyRecord",
            "description": "Records at or below a level.",
            "params": [
                {
                    "name": "max_level",
                    "type": "number",
                    "field": "level",
                    "op": "lte",
                    "min": 0,
                    "max": 10,
                }
            ],
        },
        SCORE: {
            "model": "tests.PolicyRecord",
            "description": "Records at or below a score.",
            "params": [
                {
                    "name": "max_score",
                    "type": "number",
                    "field": "score",
                    "op": "lte",
                    "min": 0.0,
                    "max": 1.0,
                }
            ],
        },
        STATUS: {
            "model": "tests.PolicyRecord",
            "description": "Records with a given status.",
            "params": [
                {
                    "name": "status",
                    "type": "choice",
                    "field": "status",
                    "choices": ["new", "done"],
                }
            ],
        },
        WINDOW: {
            "model": "tests.SampleRecord",
            "description": "Sample records inside an age window.",
            "params": [
                {**SAMPLE_AGE, "name": "older_than_seconds"},
                {**SAMPLE_AGE, "name": "newer_than_seconds", "op": "gte"},
            ],
        },
    }
    base.update(extra)
    return base


ALLOWED = ["tests.SampleRecord", "tests.PolicyRecord"]
FINGERPRINT_LABEL = {"tests.SampleRecord": ["label"]}


def harness_settings(**overrides: Any) -> Any:
    """override_settings with the harness policies and allowlist applied."""
    values: dict[str, Any] = {
        "DB_PURGE_MCP_POLICIES": _policies(),
        "DB_PURGE_MCP_ALLOWED_MODELS": ALLOWED,
        "DB_PURGE_MCP_MAX_ROWS": 10000,
        "DB_PURGE_MCP_FINGERPRINT_FIELDS": {},
    }
    values.update(overrides)
    return override_settings(**values)


def make_samples(count: int, age_days: int = 10) -> None:
    """Create count SampleRecord rows created age_days ago."""
    created = timezone.now() - timedelta(days=age_days)
    SampleRecord.objects.bulk_create(
        SampleRecord(created_at=created, label=f"s{index}") for index in range(count)
    )


def make_levels(count: int, level: int = 1, score: float = 0.25) -> None:
    """Create count PolicyRecord rows with the given level and score."""
    now = timezone.now()
    PolicyRecord.objects.bulk_create(
        PolicyRecord(level=level, score=score, created_at=now, due_on=now.date())
        for _ in range(count)
    )


STALE_PARAMS: dict[str, Any] = {"older_than_seconds": 3600}


def _token(policy: str = STALE, params: dict[str, Any] | None = STALE_PARAMS) -> str:
    """Preview and return the confirmation token."""
    return preview_purge_candidates(policy, params)["confirmation_token"]


def _refusal(fn: Callable[[], object]) -> str | None:
    """Return the name of the PurgePolicyError subclass fn raises, else None."""
    try:
        fn()
    except PurgePolicyError as exc:
        return type(exc).__name__
    return None


def _tool_message(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def _call_tool(tool: str, arguments: dict[str, Any]) -> Any:
    """Call a tool through an in-memory fastmcp client without raising."""

    async def go() -> Any:
        async with fastmcp.Client(purge_mcp_server.mcp) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)

    return asyncio.run(go())


# token_lifecycle


@attack("token-forged")
def token_forged() -> Outcome:
    make_samples(3)
    with harness_settings():
        # A real preview exists, so the store is not empty for the forgery.
        _token()
        forged = ["forged-token", "A" * 43, "", "../token", "\x00"]
        for value in forged:
            try:
                execute_purge_candidates(STALE, value, STALE_PARAMS)
            except PurgePolicyError as exc:
                if str(exc) != TOKEN_ERROR_MESSAGE:
                    return not_blocked("forged token rejected with a different error")
            else:
                return not_blocked("a forged token deleted rows")
    remaining = SampleRecord.objects.count()
    if remaining != 3:
        return not_blocked(f"{remaining} of 3 rows remain after forged tokens")
    return blocked(
        f"{len(forged)} forged tokens rejected with the opaque error, "
        "3 of 3 rows remain"
    )


@attack("token-expired")
def token_expired() -> Outcome:
    make_samples(3)
    with harness_settings():
        token = _token()
        later = timezone.now() + TOKEN_TTL + timedelta(minutes=1)
        with patch("django.utils.timezone.now", return_value=later):
            refusal = _refusal(
                lambda: execute_purge_candidates(STALE, token, STALE_PARAMS)
            )
    remaining = SampleRecord.objects.count()
    if refusal is None or remaining != 3:
        return not_blocked(f"expired token result {refusal}, {remaining} of 3 remain")
    return blocked("token rejected 1 minute past the TTL, 3 of 3 rows remain")


@attack("token-replay-after-execute")
def token_replay() -> Outcome:
    make_samples(3)
    with harness_settings():
        token = _token()
        deleted = execute_purge_candidates(STALE, token, STALE_PARAMS)["deleted_count"]
        make_samples(3)
        refusal = _refusal(lambda: execute_purge_candidates(STALE, token, STALE_PARAMS))
    remaining = SampleRecord.objects.count()
    if deleted != 3 or refusal is None or remaining != 3:
        return not_blocked(f"replay result {refusal}, {remaining} of 3 new rows remain")
    return blocked("first execute deleted 3 rows, replay rejected, 3 new rows remain")


@attack("token-wrong-policy-burned")
def token_wrong_policy() -> Outcome:
    make_samples(3)
    with harness_settings():
        token = _token()
        wrong = _refusal(lambda: execute_purge_candidates(OTHER, token, STALE_PARAMS))
        right = _refusal(lambda: execute_purge_candidates(STALE, token, STALE_PARAMS))
    remaining = SampleRecord.objects.count()
    if wrong is None or right is None or remaining != 3:
        return not_blocked(f"wrong {wrong}, correct {right}, {remaining} of 3 remain")
    return blocked(
        "mismatched call rejected, correct call then rejected, 3 of 3 remain"
    )


# parameter_binding


@attack("binding-different-value")
def binding_different_value() -> Outcome:
    make_samples(1, age_days=10)
    make_samples(1, age_days=1)
    with harness_settings():
        narrow = {"older_than_seconds": 5 * 86400}
        token = _token(STALE, narrow)
        wider = {"older_than_seconds": 3600}
        refusal = _refusal(lambda: execute_purge_candidates(STALE, token, wider))
    remaining = SampleRecord.objects.count()
    if refusal is None or remaining != 2:
        return not_blocked(f"wider execute result {refusal}, {remaining} of 2 remain")
    return blocked("token for a 5 day window rejected a 1 hour window, 2 of 2 remain")


@attack("binding-extra-param")
def binding_extra_param() -> Outcome:
    make_samples(3)
    with harness_settings():
        extra = {**STALE_PARAMS, "label": "s0"}
        token = _token()
        at_execute = _refusal(lambda: execute_purge_candidates(STALE, token, extra))
        at_preview = _refusal(lambda: preview_purge_candidates(STALE, extra))
    remaining = SampleRecord.objects.count()
    if at_execute is None or at_preview is None or remaining != 3:
        return not_blocked(f"extra key: execute {at_execute}, preview {at_preview}")
    return blocked("extra key rejected at preview and at execute, 3 of 3 remain")


@attack("binding-type-bool")
def binding_type_bool() -> Outcome:
    make_samples(3)
    with harness_settings():
        token = _token()
        at_execute = _refusal(
            lambda: execute_purge_candidates(STALE, token, {"older_than_seconds": True})
        )
        at_preview = _refusal(
            lambda: preview_purge_candidates(STALE, {"older_than_seconds": True})
        )
        level = _refusal(lambda: preview_purge_candidates(LEVEL, {"max_level": False}))
    remaining = SampleRecord.objects.count()
    if None in (at_execute, at_preview, level) or remaining != 3:
        return not_blocked(f"bool: {at_execute}, {at_preview}, {level}")
    return blocked("True and False rejected for integer params, 3 of 3 remain")


@attack("binding-type-float-for-integer")
def binding_type_float() -> Outcome:
    make_samples(3)
    make_levels(3, level=1)
    with harness_settings():
        level = _refusal(lambda: preview_purge_candidates(LEVEL, {"max_level": 5.0}))
        age = _refusal(
            lambda: preview_purge_candidates(STALE, {"older_than_seconds": 3600.0})
        )
    if level is None or age is None or len(_TOKENS) != 0:
        return not_blocked(f"float: integer_only {level}, age {age}")
    return blocked("5.0 and 3600.0 rejected for integer params, no token issued")


@attack("binding-type-numeric-string")
def binding_type_numeric_string() -> Outcome:
    make_levels(3, score=0.25)
    with harness_settings():
        results = [
            _refusal(lambda v=value: preview_purge_candidates(SCORE, {"max_score": v}))
            for value in ("0.5", "1e0", " 1", "0x1")
        ]
    if None in results or len(_TOKENS) != 0:
        return not_blocked(f"numeric string results {results}")
    return blocked("4 numeric strings rejected for a number param, no token issued")


@attack("binding-out-of-bounds")
def binding_out_of_bounds() -> Outcome:
    make_samples(3)
    make_levels(3)
    cases = [
        (STALE, {"older_than_seconds": 0}),
        (STALE, {"older_than_seconds": -1}),
        (STALE, {"older_than_seconds": 315360001}),
        (LEVEL, {"max_level": 11}),
        (LEVEL, {"max_level": -1}),
        (SCORE, {"max_score": 1.5}),
        (SCORE, {"max_score": float("nan")}),
        (SCORE, {"max_score": float("inf")}),
    ]
    with harness_settings():
        results = [
            _refusal(lambda n=name, p=params: preview_purge_candidates(n, p))
            for name, params in cases
        ]
    if None in results or len(_TOKENS) != 0:
        return not_blocked(f"out of bounds results {results}")
    return blocked(f"{len(cases)} out-of-bounds or non-finite values rejected")


# row_set_fingerprint


@attack("fingerprint-phantom-insert")
def fingerprint_phantom_insert() -> Outcome:
    make_samples(2)
    with harness_settings():
        token = _token()
        make_samples(1)
        refusal = _refusal(lambda: execute_purge_candidates(STALE, token, STALE_PARAMS))
    remaining = SampleRecord.objects.count()
    if refusal != PurgeRowSetChangedError.__name__ or remaining != 3:
        return not_blocked(f"phantom insert result {refusal}, {remaining} of 3 remain")
    return blocked("row set drift error after an insert, 3 of 3 rows remain")


@attack("fingerprint-row-deleted")
def fingerprint_row_deleted() -> Outcome:
    make_samples(3)
    with harness_settings():
        token = _token()
        SampleRecord.objects.order_by("pk").first().delete()
        refusal = _refusal(lambda: execute_purge_candidates(STALE, token, STALE_PARAMS))
    remaining = SampleRecord.objects.count()
    if refusal != PurgeRowSetChangedError.__name__ or remaining != 2:
        return not_blocked(f"row deleted result {refusal}, {remaining} of 2 remain")
    return blocked("row set drift error after a delete, 2 of 2 rows remain")


@attack("fingerprint-in-place-mutation")
def fingerprint_in_place_mutation() -> Outcome:
    make_samples(3)
    with harness_settings(DB_PURGE_MCP_FINGERPRINT_FIELDS=FINGERPRINT_LABEL):
        token = _token()
        SampleRecord.objects.filter(label="s0").update(label="mutated")
        refusal = _refusal(lambda: execute_purge_candidates(STALE, token, STALE_PARAMS))
    remaining = SampleRecord.objects.count()
    if refusal != PurgeRowSetChangedError.__name__ or remaining != 3:
        return not_blocked(f"mutation result {refusal}, {remaining} of 3 remain")
    return blocked("row set drift error after an in-place label change, 3 of 3 remain")


# allowlist_scope_escape


@attack("allowlist-model-removed-after-preview")
def allowlist_model_removed() -> Outcome:
    make_samples(3)
    with harness_settings():
        token = _token()
    with harness_settings(DB_PURGE_MCP_ALLOWED_MODELS=["tests.PolicyRecord"]):
        refusal = _refusal(lambda: execute_purge_candidates(STALE, token, STALE_PARAMS))
    remaining = SampleRecord.objects.count()
    if refusal is None or remaining != 3:
        return not_blocked(f"withdrawn model result {refusal}, {remaining} of 3 remain")
    return blocked("execute failed closed after the model left the allowlist")


@attack("allowlist-empty")
def allowlist_empty() -> Outcome:
    make_samples(3)
    with harness_settings(DB_PURGE_MCP_ALLOWED_MODELS=[]):
        at_preview = _refusal(lambda: preview_purge_candidates(STALE, STALE_PARAMS))
        at_list = _refusal(describe_policies)
        at_execute = _refusal(
            lambda: execute_purge_candidates(STALE, "forged", STALE_PARAMS)
        )
    remaining = SampleRecord.objects.count()
    if None in (at_preview, at_list, at_execute) or remaining != 3:
        return not_blocked(f"empty allowlist: {at_preview}, {at_list}, {at_execute}")
    return blocked("list, preview and execute all refused with an empty allowlist")


@attack("allowlist-policy-model-outside")
def allowlist_policy_model_outside() -> Outcome:
    make_samples(3)
    make_levels(3)
    outside = {
        "model": "tests.PolicyRecord",
        "description": "Targets a model outside the allowlist.",
        "params": [
            {
                "name": "max_level",
                "type": "number",
                "field": "level",
                "op": "lte",
                "min": 0,
                "max": 10,
            }
        ],
    }
    with harness_settings(
        DB_PURGE_MCP_POLICIES={STALE: _policies()[STALE], "outside": outside},
        DB_PURGE_MCP_ALLOWED_MODELS=["tests.SampleRecord"],
    ):
        bad = _refusal(lambda: preview_purge_candidates("outside", {"max_level": 5}))
        good = _refusal(lambda: preview_purge_candidates(STALE, STALE_PARAMS))
    remaining = PolicyRecord.objects.count()
    if bad is None or good is None or remaining != 3 or len(_TOKENS) != 0:
        return not_blocked(f"outside model: {bad}, valid sibling {good}")
    return blocked("one out-of-allowlist policy disabled every policy, no token issued")


def _schema_escape(argument: str, value: object) -> Outcome:
    """Send an extra argument to preview and execute through the client."""
    make_samples(3)
    with harness_settings():
        token = _token()
        previewed = _call_tool(
            "preview_purge",
            {"policy_name": STALE, "params": STALE_PARAMS, argument: value},
        )
        executed = _call_tool(
            "execute_purge",
            {
                "policy_name": STALE,
                "confirmation_token": token,
                "params": STALE_PARAMS,
                argument: value,
            },
        )
    remaining = SampleRecord.objects.count()
    named = argument in _tool_message(previewed)
    if not (previewed.is_error and executed.is_error) or remaining != 3:
        return not_blocked(f"{argument} argument accepted, {remaining} of 3 remain")
    if not named:
        return not_blocked(f"{argument} rejected for an unrelated reason")
    return blocked(
        f"schema rejected the {argument} argument on both tools, 3 of 3 rows remain"
    )


@attack("scope-model-argument")
def scope_model_argument() -> Outcome:
    return _schema_escape("model", "tests.PolicyRecord")


@attack("scope-filter-argument")
def scope_filter_argument() -> Outcome:
    return _schema_escape("filter", "1=1")


@attack("scope-select-argument")
def scope_select_argument() -> Outcome:
    return _schema_escape("select", {"pk__gte": 0})


# row_cap


@attack("cap-preview-exceeds")
def cap_preview_exceeds() -> Outcome:
    make_samples(3)
    with harness_settings(DB_PURGE_MCP_MAX_ROWS=2):
        refusal = _refusal(lambda: preview_purge_candidates(STALE, STALE_PARAMS))
        try:
            preview_purge_candidates(STALE, STALE_PARAMS)
        except PurgeRowCapError as exc:
            counted = exc.row_count
        else:
            counted = -1
    if refusal != PurgeRowCapError.__name__ or counted != 3 or len(_TOKENS) != 0:
        return not_blocked(f"over cap preview result {refusal}, tokens {len(_TOKENS)}")
    return blocked("preview of 3 rows refused under a cap of 2, no token issued")


@attack("cap-policy-max-rows-lower")
def cap_policy_max_rows_lower() -> Outcome:
    capped = {**_policies()[STALE], "max_rows": 2}
    with harness_settings(
        DB_PURGE_MCP_POLICIES={STALE: capped}, DB_PURGE_MCP_MAX_ROWS=10
    ):
        make_samples(2)
        within = _refusal(lambda: preview_purge_candidates(STALE, STALE_PARAMS))
        _TOKENS.clear()
        make_samples(1)
        over = _refusal(lambda: preview_purge_candidates(STALE, STALE_PARAMS))
    if within is not None or over != PurgeRowCapError.__name__ or len(_TOKENS) != 0:
        return not_blocked(f"policy cap 2 under global 10: 2 rows {within}, 3 {over}")
    return blocked(
        "policy max_rows of 2 allowed 2 rows and refused 3 under a global 10"
    )


@attack("cap-global-zero-fails-closed")
def cap_global_zero() -> Outcome:
    make_samples(3)
    with harness_settings():
        token = _token()
    with harness_settings(DB_PURGE_MCP_MAX_ROWS=0):
        at_preview = _refusal(lambda: preview_purge_candidates(STALE, STALE_PARAMS))
        at_list = _refusal(describe_policies)
        at_execute = _refusal(
            lambda: execute_purge_candidates(STALE, token, STALE_PARAMS)
        )
    remaining = SampleRecord.objects.count()
    if None in (at_preview, at_list, at_execute) or remaining != 3:
        return not_blocked(f"zero cap: {at_preview}, {at_list}, {at_execute}")
    return blocked("cap of 0 refused list, preview and execute, 3 of 3 rows remain")


@attack("cap-aggregate-bypass")
def cap_aggregate_bypass() -> Outcome:
    cycles, per_window, cap = 5, 2, 2
    for cycle in range(1, cycles + 1):
        make_samples(per_window, age_days=10 * cycle)
    deleted = 0
    with harness_settings(DB_PURGE_MCP_MAX_ROWS=cap):
        # A whole-table purge is refused, as the cap intends.
        whole = _refusal(
            lambda: preview_purge_candidates(
                WINDOW, {"older_than_seconds": 3600, "newer_than_seconds": 10**8}
            )
        )
        # Narrow windows each hold at most the cap, so every cycle passes.
        for cycle in range(1, cycles + 1):
            params = {
                "older_than_seconds": (10 * cycle - 1) * 86400,
                "newer_than_seconds": (10 * cycle + 1) * 86400,
            }
            token = _token(WINDOW, params)
            result = execute_purge_candidates(WINDOW, token, params)
            deleted += result["deleted_count"]
    total = cycles * per_window
    if whole != PurgeRowCapError.__name__:
        return blocked("the single whole-table purge was refused")
    if deleted > cap:
        return not_blocked(
            f"deleted {deleted} rows over {cycles} cycles under a per-call cap of {cap}"
        )
    return blocked(f"total deleted stayed within the cap of {cap} across {total} rows")


# protocol_injection


def _payloads(*groups: str) -> list[str]:
    return [value for group in groups for value in PAYLOADS[group]]


@attack("injection-policy-name-payloads")
def injection_policy_names() -> Outcome:
    make_samples(3)
    names = _payloads(
        "prompt_injection", "sql", "path_traversal", "crlf", "nul", "very_long"
    )
    with harness_settings():
        for name in names:
            previewed = _call_tool("preview_purge", {"policy_name": name})
            executed = _call_tool(
                "execute_purge",
                {"policy_name": name, "confirmation_token": "forged"},
            )
            if not (previewed.is_error and executed.is_error):
                return not_blocked("a payload policy name was accepted")
            if name[:24] in _tool_message(previewed) + _tool_message(executed):
                return not_blocked("an error message echoed the payload")
    remaining = SampleRecord.objects.count()
    if remaining != 3 or len(_TOKENS) != 0:
        return not_blocked(f"{remaining} of 3 rows remain after policy name payloads")
    return blocked(
        f"{len(names)} payload policy names rejected on both tools without echo"
    )


@attack("injection-param-value-payloads")
def injection_param_values() -> Outcome:
    make_samples(3)
    make_levels(3)
    values = _payloads("prompt_injection", "sql", "path_traversal", "crlf", "nul")
    with harness_settings():
        for value in values:
            for policy, param in ((STALE, "older_than_seconds"), (STATUS, "status")):
                result = _call_tool(
                    "preview_purge", {"policy_name": policy, "params": {param: value}}
                )
                if not result.is_error:
                    return not_blocked(f"payload accepted as {param}")
    remaining = SampleRecord.objects.count() + PolicyRecord.objects.count()
    if remaining != 6 or len(_TOKENS) != 0:
        return not_blocked(f"{remaining} of 6 rows remain after param payloads")
    return blocked(
        f"{len(values)} payloads rejected as integer and choice values, no token issued"
    )


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _captured_calls() -> Iterator[_Collector]:
    """Collect every dbpurge.mcp record, whatever the logging setup."""
    handler = _Collector()
    logger = purge_mcp_server.call_logger
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@attack("injection-crlf-log-forgery")
def injection_crlf_log_forgery() -> Outcome:
    forged = "execute_purge consumed policy='stale_samples' rows=999"
    names = PAYLOADS["crlf"]
    with harness_settings(), _captured_calls() as captured:
        for name in names:
            _call_tool("preview_purge", {"policy_name": name})
            _call_tool(
                "execute_purge",
                {"policy_name": name, "confirmation_token": "forged"},
            )
    broken = [m for m in captured.messages if "\n" in m or "\r" in m]
    if broken or not captured.messages:
        return not_blocked(f"{len(broken)} log records contained a line break")
    if any(m.startswith(forged) for m in captured.messages):
        return not_blocked("a forged log line was emitted")
    return blocked(
        f"{len(captured.messages)} call-log records, none containing CR or LF"
    )


@attack("injection-oversized-string")
def injection_oversized_string() -> Outcome:
    make_samples(3)
    huge = "A" * OVERSIZED_LENGTH
    with harness_settings(), _captured_calls() as captured:
        as_name = _call_tool("preview_purge", {"policy_name": huge})
        as_param = _call_tool(
            "preview_purge",
            {"policy_name": STALE, "params": {"older_than_seconds": huge}},
        )
        as_token = _call_tool(
            "execute_purge",
            {
                "policy_name": STALE,
                "confirmation_token": huge,
                "params": STALE_PARAMS,
            },
        )
    remaining = SampleRecord.objects.count()
    longest = max((len(m) for m in captured.messages), default=0)
    if not (as_name.is_error and as_param.is_error and as_token.is_error):
        return not_blocked("an oversized argument was accepted")
    if remaining != 3:
        return not_blocked(f"{remaining} of 3 rows remain")
    if longest > LOG_RECORD_LIMIT:
        return not_blocked(
            f"a 1 MiB argument was rejected but written to the call log in full "
            f"(longest record over {LOG_RECORD_LIMIT} characters)"
        )
    return blocked(
        f"1 MiB name, param and token rejected, log records under {LOG_RECORD_LIMIT}"
    )


@attack("injection-homoglyph-policy-name")
def injection_homoglyph() -> Outcome:
    make_samples(3)
    names = PAYLOADS["homoglyphs"]
    with harness_settings():
        for name in names:
            previewed = _call_tool("preview_purge", {"policy_name": name})
            if not previewed.is_error:
                return not_blocked("a lookalike policy name was accepted")
        # A control that never touches the database: the real name is listed.
        listed = _call_tool("list_policies", {})
    remaining = SampleRecord.objects.count()
    if listed.is_error or STALE not in _tool_message(listed) or remaining != 3:
        return not_blocked("control listing of the real name failed")
    return blocked(
        f"{len(names)} lookalike names rejected, the real name is still listed"
    )
