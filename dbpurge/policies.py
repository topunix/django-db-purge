"""
Shared guards for the MCP purge server.

This module has no fastmcp import, so it works without the [mcp] extra.
"""

import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from django.apps import apps
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db.models import (
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    FloatField,
    IntegerField,
    Model,
    Q,
    QuerySet,
    TextField,
)
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 10000


class PurgePolicyError(Exception):
    """Raised when preview/execute parameters fail validation."""


class PurgeRowSetChangedError(PurgePolicyError):
    """Raised when the matched row set differs between preview and execute."""


def is_allowed_model(model: type[Model]) -> bool:
    """
    Return whether model is listed in DB_PURGE_MCP_ALLOWED_MODELS.

    Matched case-insensitively. The recommended format is Django's own
    "app_label.ModelName" label (class-cased, e.g. "tests.SampleRecord"),
    but list_purge_candidates surfaces model names in lowercase, so
    either casing is accepted rather than silently rejecting whichever
    one an operator copies in.
    """
    allowed = {
        name.lower() for name in getattr(settings, "DB_PURGE_MCP_ALLOWED_MODELS", [])
    }
    return model._meta.label.lower() in allowed


POLICY_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_PARAMS = 2

ParamType = Literal["age_seconds", "number", "choice"]

_POLICY_KEYS = {
    "model",
    "description",
    "action",
    "select",
    "exclude",
    "params",
    "max_rows",
}
_PARAM_KEYS: dict[str, set[str]] = {
    "age_seconds": {"name", "type", "field", "op", "min", "max"},
    "number": {"name", "type", "field", "op", "min", "max"},
    "choice": {"name", "type", "field", "choices"},
}
_REQUIRED_PARAM_KEYS: dict[str, set[str]] = {
    "age_seconds": {"name", "type", "field", "min", "max"},
    "number": {"name", "type", "field", "op", "min", "max"},
    "choice": {"name", "type", "field", "choices"},
}
_AGE_OPS = ("lte", "gte")
_NUMBER_OPS = ("lt", "lte", "gt", "gte")


@dataclass(frozen=True)
class Param:
    """One validated, agent-tunable parameter of a policy."""

    name: str
    type: ParamType
    field: str
    op: str | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    choices: tuple[str, ...] | tuple[int, ...] | None = None


@dataclass(frozen=True)
class Policy:
    """One validated purge policy. Built only by load_policies()."""

    name: str
    model: type[Model]
    description: str
    action: str
    select: Q | None
    exclude: Q | None
    params: tuple[Param, ...]
    max_rows: int | None


def get_max_rows_cap() -> int:
    """Return DB_PURGE_MCP_MAX_ROWS, or the default when it is unset."""
    cap = getattr(settings, "DB_PURGE_MCP_MAX_ROWS", DEFAULT_MAX_ROWS)
    if not _is_int(cap) or cap <= 0:
        raise ImproperlyConfigured("DB_PURGE_MCP_MAX_ROWS must be a positive integer")
    return int(cap)


def _is_int(value: object) -> bool:
    """Return whether value is an int and not a bool (bool subclasses int)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    """Return whether value is a finite int or float, never a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _bad(policy_name: str, message: str) -> ImproperlyConfigured:
    return ImproperlyConfigured(f"DB_PURGE_MCP_POLICIES[{policy_name!r}]: {message}")


def _check_age_seconds_max(policy_name: str, maximum: int) -> None:
    """Require that now minus maximum seconds is a representable datetime."""
    try:
        cutoff = timezone.now() - timedelta(seconds=maximum)
    except OverflowError:
        raise _bad(
            policy_name, "age_seconds max is too large to subtract from now"
        ) from None
    if cutoff.year < 1:
        raise _bad(policy_name, "age_seconds max is too large to subtract from now")


def _load_param(policy_name: str, model: type[Model], raw: object) -> Param:
    if not isinstance(raw, dict):
        raise _bad(policy_name, "each param must be a dict")
    param_type = raw.get("type")
    if param_type not in _PARAM_KEYS:
        raise _bad(policy_name, "param type must be age_seconds, number or choice")
    unknown = set(raw) - _PARAM_KEYS[param_type]
    if unknown:
        raise _bad(
            policy_name,
            f"unknown param keys for {param_type}: {sorted(unknown, key=str)}",
        )
    missing = _REQUIRED_PARAM_KEYS[param_type] - set(raw)
    if missing:
        raise _bad(policy_name, f"param is missing keys: {sorted(missing)}")

    name = raw["name"]
    if not isinstance(name, str) or not POLICY_NAME_RE.fullmatch(name):
        raise _bad(policy_name, f"param name must match {POLICY_NAME_RE.pattern}")
    field_name = raw["field"]
    if not isinstance(field_name, str) or "__" in field_name:
        raise _bad(policy_name, f"param {name!r}: field must be a plain field name")
    field = _concrete_field(policy_name, model, name, field_name)

    if param_type == "choice":
        return Param(
            name=name,
            type="choice",
            field=field_name,
            choices=_load_choices(policy_name, name, field, raw["choices"]),
        )

    minimum, maximum = raw["min"], raw["max"]
    if param_type == "age_seconds":
        if not isinstance(field, DateField):
            raise _bad(policy_name, f"param {name!r}: field must be a date or datetime")
        op = raw.get("op", "lte")
        if op not in _AGE_OPS:
            raise _bad(policy_name, f"param {name!r}: op must be lte or gte")
        if not _is_int(minimum) or not _is_int(maximum):
            raise _bad(policy_name, f"param {name!r}: min and max must be integers")
        if not 0 < minimum <= maximum:
            raise _bad(policy_name, f"param {name!r}: need 0 < min <= max")
        _check_age_seconds_max(policy_name, maximum)
    else:
        if not isinstance(field, (IntegerField, FloatField, DecimalField)):
            raise _bad(policy_name, f"param {name!r}: field must be numeric")
        op = raw["op"]
        if op not in _NUMBER_OPS:
            raise _bad(policy_name, f"param {name!r}: op must be lt, lte, gt or gte")
        if not _is_finite_number(minimum) or not _is_finite_number(maximum):
            raise _bad(
                policy_name, f"param {name!r}: min and max must be finite numbers"
            )
        if minimum > maximum:
            raise _bad(policy_name, f"param {name!r}: need min <= max")
    return Param(
        name=name,
        type=param_type,
        field=field_name,
        op=op,
        minimum=minimum,
        maximum=maximum,
    )


def _concrete_field(
    policy_name: str, model: type[Model], param_name: str, field_name: str
) -> Any:
    """Return the model field, rejecting missing, relational and pk fields."""
    try:
        field = model._meta.get_field(field_name)
    except FieldDoesNotExist:
        raise _bad(
            policy_name, f"param {param_name!r}: field is not on the model"
        ) from None
    if not getattr(field, "concrete", False) or field.is_relation:
        raise _bad(
            policy_name, f"param {param_name!r}: field must be a concrete column"
        )
    if field.primary_key:
        raise _bad(policy_name, f"param {param_name!r}: field must not be the pk")
    return field


def _load_choices(
    policy_name: str, name: str, field: Any, raw: object
) -> tuple[str, ...] | tuple[int, ...]:
    if not isinstance(raw, list) or not raw:
        raise _bad(policy_name, f"param {name!r}: choices must be a non-empty list")
    str_items = [item for item in raw if isinstance(item, str)]
    int_items = [
        item for item in raw if isinstance(item, int) and not isinstance(item, bool)
    ]
    if len(str_items) == len(raw):
        if len(set(str_items)) != len(str_items):
            raise _bad(policy_name, f"param {name!r}: choices must not repeat")
        if not isinstance(field, (CharField, TextField)):
            raise _bad(policy_name, f"param {name!r}: str choices need a char field")
        return tuple(str_items)
    if len(int_items) == len(raw):
        if len(set(int_items)) != len(int_items):
            raise _bad(policy_name, f"param {name!r}: choices must not repeat")
        if not isinstance(field, IntegerField):
            raise _bad(policy_name, f"param {name!r}: int choices need an int field")
        return tuple(int_items)
    raise _bad(policy_name, f"param {name!r}: choices must be all str or all int")


_UPPER_OPS = ("lt", "lte")


def _check_same_field_params(policy_name: str, params: tuple[Param, ...]) -> None:
    """Allow two params on one field only as an opposite-direction pair."""
    seen: dict[str, Param] = {}
    for param in params:
        other = seen.get(param.field)
        if other is None:
            seen[param.field] = param
            continue
        if param.type != other.type or param.type == "choice":
            raise _bad(
                policy_name,
                "two params on one field must share a range type (not choice)",
            )
        if (param.op in _UPPER_OPS) == (other.op in _UPPER_OPS):
            raise _bad(policy_name, "two params on one field need opposite ops")


def _is_empty_q(condition: Q) -> bool:
    """Return whether condition has no real condition in it, negated or not."""
    return all(
        isinstance(child, Q) and _is_empty_q(child) for child in condition.children
    )


def _select_queryset(
    model: type[Model], select: Q | None, exclude: Q | None
) -> QuerySet[Any]:
    queryset: QuerySet[Any] = model._default_manager.all()
    if select is not None:
        queryset = queryset.filter(select)
    if exclude is not None:
        queryset = queryset.exclude(exclude)
    return queryset


def _load_policy(name: object, raw: object) -> Policy:
    if not isinstance(name, str) or not POLICY_NAME_RE.fullmatch(name):
        raise ImproperlyConfigured(
            f"DB_PURGE_MCP_POLICIES: policy names must match {POLICY_NAME_RE.pattern}"
        )
    if not isinstance(raw, dict):
        raise _bad(name, "a policy must be a dict")
    unknown = set(raw) - _POLICY_KEYS
    if unknown:
        raise _bad(name, f"unknown keys: {sorted(unknown, key=str)}")
    for key in ("model", "description"):
        if key not in raw:
            raise _bad(name, f"missing required key {key!r}")

    description = raw["description"]
    if not isinstance(description, str) or not description.strip():
        raise _bad(name, "description must be a non-empty string")
    if raw.get("action", "delete") != "delete":
        raise _bad(name, "action must be 'delete'")

    label = raw["model"]
    try:
        if not isinstance(label, str):
            raise LookupError(label)
        model: type[Model] = apps.get_model(label)
    except (LookupError, ValueError):
        raise _bad(name, "model must be an installed 'app_label.ModelName'") from None
    if not is_allowed_model(model):
        raise _bad(name, f"{model._meta.label} is not in DB_PURGE_MCP_ALLOWED_MODELS")

    select, exclude = raw.get("select"), raw.get("exclude")
    for key, value in (("select", select), ("exclude", exclude)):
        if key in raw and not isinstance(value, Q):
            raise _bad(name, f"{key} must be a django.db.models.Q")
        if isinstance(value, Q) and _is_empty_q(value):
            raise _bad(name, f"{key} must not be an empty Q")

    raw_params = raw.get("params", [])
    if not isinstance(raw_params, list) or len(raw_params) > MAX_PARAMS:
        raise _bad(name, f"params must be a list of at most {MAX_PARAMS}")
    params = tuple(_load_param(name, model, item) for item in raw_params)
    if select is None and not params:
        raise _bad(name, "a policy needs select or at least one param")
    if len({param.name for param in params}) != len(params):
        raise _bad(name, "param names must be unique within a policy")
    _check_same_field_params(name, params)

    max_rows: int | None = None
    if "max_rows" in raw:
        candidate = raw["max_rows"]
        if not isinstance(candidate, int) or isinstance(candidate, bool):
            raise _bad(name, "max_rows must be a positive integer")
        if candidate <= 0:
            raise _bad(name, "max_rows must be a positive integer")
        if candidate > get_max_rows_cap():
            raise _bad(name, "max_rows must not exceed DB_PURGE_MCP_MAX_ROWS")
        max_rows = candidate

    # Compile now so a Q naming a missing field fails at load, not at preview.
    # str() on a query builds the SQL without executing it.
    try:
        str(_select_queryset(model, select, exclude).query)
    except Exception as exc:
        raise _bad(name, f"select or exclude does not compile: {exc}") from None

    return Policy(
        name=name,
        model=model,
        description=description,
        action="delete",
        select=select,
        exclude=exclude,
        params=params,
        max_rows=max_rows,
    )


def load_policies() -> dict[str, Policy]:
    """
    Read and validate DB_PURGE_MCP_POLICIES, failing closed.

    Called on every tool call and once at server start. Any rule
    violation raises ImproperlyConfigured and nothing is returned, so
    one bad policy disables every policy rather than being skipped.
    An unset or empty setting returns an empty dict, meaning nothing is
    purgeable via MCP.
    """
    raw = getattr(settings, "DB_PURGE_MCP_POLICIES", None)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ImproperlyConfigured("DB_PURGE_MCP_POLICIES must be a dict")
    return {name: _load_policy(name, value) for name, value in raw.items()}


ParamValue = int | float | str

_POLICIES_UNAVAILABLE = "Purge policies are not available"


def get_policy(name: object) -> Policy:
    """
    Load policies afresh and return the one called name.

    Loading on every call means the allowlist and the config are
    re-checked each time, with no cache to go stale. If the config is
    invalid the client gets one opaque message and the detail goes to
    the server log only. An unknown name is rejected without echoing
    it, since it is agent-supplied text.
    """
    try:
        policies = load_policies()
    except ImproperlyConfigured as exc:
        logger.error("DB_PURGE_MCP_POLICIES failed validation: %s", exc)
        raise PurgePolicyError(_POLICIES_UNAVAILABLE) from None
    policy = policies.get(name) if isinstance(name, str) else None
    if policy is None:
        raise PurgePolicyError("Unknown purge policy. Call list_policies first.")
    return policy


def validate_request(
    policy: Policy, params: Mapping[str, object] | None
) -> dict[str, ParamValue]:
    """
    Validate agent-supplied params against the policy, before any query.

    Shared guard for preview_purge and execute_purge. None is the same
    as {}. The keys must equal the declared param names exactly: there
    are no defaults, and extra keys are rejected. Values are checked as
    received, by exact type: a bool is never a number, NaN and Infinity
    are never accepted, and a choice must equal a declared entry. A
    same-field pair must describe a non-empty window. Returns the
    validated values, which are what the confirmation token binds to.
    Error messages name only declared params, never supplied text.
    """
    supplied: Mapping[str, object] = {} if params is None else params
    if not isinstance(supplied, Mapping):
        raise PurgePolicyError("params must be an object")
    declared = [param.name for param in policy.params]
    if set(supplied) != set(declared):
        raise PurgePolicyError(
            f"params must be exactly: {', '.join(declared) or '(none)'}"
        )
    validated = {
        param.name: _validate_value(param, supplied[param.name])
        for param in policy.params
    }
    _check_window(policy.params, validated)
    return validated


def _validate_value(param: Param, value: object) -> ParamValue:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise PurgePolicyError(f"param {param.name!r} has an invalid value")
    if param.type == "choice":
        choices = param.choices or ()
        if not choices or type(value) is not type(choices[0]) or value not in choices:
            raise PurgePolicyError(f"param {param.name!r} is not an allowed choice")
        return value
    if isinstance(value, str):
        raise PurgePolicyError(f"param {param.name!r} must be a number")
    if param.type == "age_seconds" and not isinstance(value, int):
        raise PurgePolicyError(f"param {param.name!r} must be an integer")
    if isinstance(value, float) and not math.isfinite(value):
        raise PurgePolicyError(f"param {param.name!r} must be finite")
    low, high = param.minimum, param.maximum
    if low is None or high is None or not low <= value <= high:
        raise PurgePolicyError(
            f"param {param.name!r} is out of bounds ({low} to {high})"
        )
    return value


def _as_number(value: ParamValue) -> int | float:
    if isinstance(value, str):
        raise PurgePolicyError("a range param must be a number")
    return value


def _check_window(
    params: tuple[Param, ...], validated: Mapping[str, ParamValue]
) -> None:
    """
    Reject a same-field pair whose window is inverted or empty.

    For age_seconds the upper side (lte, "older than a seconds") and the
    lower side (gte, "within b seconds") overlap only if b >= a. For
    number, the window is empty if lower > upper, or if they are equal
    and either side is strict. Equal bounds are valid only when both
    ops are inclusive, a single point.
    """
    lower: dict[str, Param] = {}
    upper: dict[str, Param] = {}
    for param in params:
        (upper if param.op in _UPPER_OPS else lower)[param.field] = param
    for field in lower.keys() & upper.keys():
        low_param, up_param = lower[field], upper[field]
        low = _as_number(validated[low_param.name])
        up = _as_number(validated[up_param.name])
        if low_param.type == "age_seconds":
            # Offsets are seconds before now, so the lower side is the
            # larger offset.
            empty = low < up
        else:
            strict = low_param.op == "gt" or up_param.op == "lt"
            empty = low > up or (low == up and strict)
        if empty:
            raise PurgePolicyError(
                f"params {low_param.name!r} and {up_param.name!r} describe an "
                "empty window"
            )


def cutoff_bound(field: Any, cutoff: datetime) -> datetime | date:
    """
    Return cutoff in the form a lookup on field should compare against.

    A DateTimeField compares against the aware datetime. A DateField
    compares against the cutoff's calendar date in the current time
    zone, so the comparison never depends on how a backend coerces a
    datetime into a date.
    """
    if isinstance(field, DateTimeField):
        return cutoff
    if timezone.is_aware(cutoff):
        local_date: date = timezone.localdate(cutoff)
        return local_date
    return cutoff.date()


def build_queryset(
    policy: Policy, validated: Mapping[str, ParamValue], now: datetime
) -> QuerySet[Any]:
    """
    Return the policy's matching rows for these validated params.

    Applies select, then exclude, then each param's filter. Preview and
    execute both call this, so they cannot disagree about which rows
    match. The conditions run in a subquery and the result selects the
    model by pk, so a select or exclude that joins a to-many relation
    still yields each row exactly once without DISTINCT, which keeps the
    result compatible with select_for_update on backends that forbid
    FOR UPDATE together with DISTINCT.
    """
    inner = _select_queryset(policy.model, policy.select, policy.exclude)
    for param in policy.params:
        value = validated[param.name]
        field = policy.model._meta.get_field(param.field)
        if param.type == "age_seconds":
            seconds = _as_number(value)
            cutoff = now - timedelta(seconds=seconds)
            inner = inner.filter(
                **{f"{param.field}__{param.op}": cutoff_bound(field, cutoff)}
            )
        elif param.type == "number":
            bound: int | float | Decimal = _as_number(value)
            if isinstance(field, DecimalField):
                bound = Decimal(str(bound))
            inner = inner.filter(**{f"{param.field}__{param.op}": bound})
        else:
            inner = inner.filter(**{param.field: value})
    return policy.model._default_manager.filter(pk__in=inner.values("pk"))


def hash_params(policy_name: str, validated: Mapping[str, ParamValue]) -> str:
    """
    Return the token binding hash for a policy name and validated params.

    Canonical JSON with sorted keys, so key order never changes the
    hash. Values are hashed as received: 5 and 5.0 give different
    hashes, which can only make a token fail to match, never match
    wrongly.
    """
    raw = json.dumps(
        {"policy": policy_name, "params": validated},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()
