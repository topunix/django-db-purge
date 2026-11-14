import hashlib
import logging
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

try:
    from typing_extensions import TypedDict
except ImportError:
    from typing import TypedDict

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Model, QuerySet
from django.utils import timezone

from dbpurge.policies import (
    Param,
    ParamValue,
    Policy,
    PurgePolicyError,
    PurgeRowSetChangedError,
    build_queryset,
    get_max_rows_cap,
    get_policies,
    get_policy,
    hash_params,
    load_policies,
    validate_request,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError
    from pydantic import StrictFloat, StrictInt, StrictStr
else:
    try:
        from pydantic import StrictFloat, StrictInt, StrictStr
    except ImportError:
        StrictInt, StrictFloat, StrictStr = int, float, str

    try:
        from fastmcp import FastMCP
        from fastmcp.exceptions import ToolError
    except ImportError:
        FastMCP = None

        class ToolError(Exception):
            """Stand-in for fastmcp.exceptions.ToolError when fastmcp is absent."""


logger = logging.getLogger(__name__)

MISSING_FASTMCP_ERROR = (
    "fastmcp is required to run the MCP server. Install it with "
    'pip install "django-db-purge[mcp]".'
)


_F = TypeVar("_F", bound=Callable[..., Any])


class _MissingMCP:
    """Stand-in for FastMCP when fastmcp is absent, so this module stays importable."""

    def tool(self, fn: _F) -> _F:
        return fn


# The stand-in only lets this module import without the [mcp] extra. handle()
# refuses to run in that case, so the cast never reaches a real tool call.
mcp: "FastMCP[Any]" = (
    FastMCP("django-db-purge")
    if FastMCP is not None
    else cast("FastMCP[Any]", _MissingMCP())
)

TOKEN_TTL = timedelta(minutes=5)
PREVIEW_SAMPLE_SIZE = 5
TOKEN_ERROR_MESSAGE = "invalid or expired confirmation token"


@dataclass(frozen=True)
class _TokenRecord:
    params_hash: str
    expires_at: datetime
    pk_list: tuple[int, ...]
    fingerprint: str
    preview_row_count: int


# In-process token store, fine for a single stdio server process where
# one client owns the whole session. If this server ever grows an HTTP
# transport (multiple worker processes or restarts between preview and
# execute), tokens must move to external shared storage (Redis, a DB
# table) since this dict would not be visible across workers.
_TOKENS: dict[str, _TokenRecord] = {}


class AgeSecondsParamInfo(TypedDict):
    """An age param: an int count of seconds between min and max."""

    name: str
    type: Literal["age_seconds"]
    op: Literal["lte", "gte"]
    min: int
    max: int


class NumberParamInfo(TypedDict):
    """A number param. integer_only means a float value is rejected."""

    name: str
    type: Literal["number"]
    op: Literal["lt", "lte", "gt", "gte"]
    min: int | float
    max: int | float
    integer_only: bool


class ChoiceParamInfo(TypedDict):
    """A choice param: the value must equal one entry of choices exactly."""

    name: str
    type: Literal["choice"]
    choices: list[str] | list[int]


ParamInfo = AgeSecondsParamInfo | NumberParamInfo | ChoiceParamInfo


class PolicyInfo(TypedDict):
    name: str
    description: str
    model: str
    action: Literal["delete"]
    max_rows: int
    params: list[ParamInfo]


def _describe_param(param: Param) -> ParamInfo:
    """Describe a param for the agent: name, type, op and bounds, no field."""
    if param.type == "choice":
        choices = param.choices or ()
        str_choices = [choice for choice in choices if isinstance(choice, str)]
        int_choices = [choice for choice in choices if isinstance(choice, int)]
        return {
            "name": param.name,
            "type": "choice",
            "choices": str_choices if len(str_choices) == len(choices) else int_choices,
        }
    minimum, maximum = param.minimum, param.maximum
    if param.type == "age_seconds":
        if not isinstance(minimum, int) or not isinstance(maximum, int):
            raise RuntimeError(f"age param {param.name!r} lost its integer bounds")
        return {
            "name": param.name,
            "type": "age_seconds",
            "op": "gte" if param.op == "gte" else "lte",
            "min": minimum,
            "max": maximum,
        }
    if param.op is None or minimum is None or maximum is None:
        raise RuntimeError(f"number param {param.name!r} lost its op or bounds")
    return {
        "name": param.name,
        "type": "number",
        "op": param.op,
        "min": minimum,
        "max": maximum,
        "integer_only": param.integer_only,
    }


def describe_policies() -> list[PolicyInfo]:
    """
    Return every configured policy as the agent is allowed to see it.

    Read-only. Never includes select, exclude, or the model fields a
    param filters on: the agent learns what a policy is called, what it
    is for, and what it may tune, but not how it selects rows.
    """
    policies = get_policies()
    cap = get_max_rows_cap()
    return [
        {
            "name": policy.name,
            "description": policy.description,
            "model": policy.model._meta.label,
            "action": "delete",
            "max_rows": policy.max_rows if policy.max_rows is not None else cap,
            "params": [_describe_param(param) for param in policy.params],
        }
        for policy in policies.values()
    ]


@mcp.tool
def list_policies() -> list[PolicyInfo]:
    """
    List the purge policies an operator has defined.

    Read-only. Call this first: preview_purge and execute_purge take a
    policy name from this list, never a model, column, or filter. Each
    entry gives the policy's description, the model it acts on, its
    action (always "delete"), and max_rows, the most rows one execute
    may delete. Each param lists the exact values you may supply:
    age_seconds is an integer between min and max (op says whether it
    selects rows older than that age, lte, or newer, gte), number is
    compared with op and must lie between min and max (integer_only
    means a float such as 5.0 is rejected), and choice must equal one
    of choices exactly. An empty list means nothing is purgeable.
    """
    try:
        return describe_policies()
    except PurgePolicyError as exc:
        raise ToolError(str(exc)) from exc


def _evict_expired_tokens() -> None:
    """Drop expired entries so _TOKENS does not grow without bound."""
    now = timezone.now()
    expired = [token for token, record in _TOKENS.items() if record.expires_at <= now]
    for token in expired:
        del _TOKENS[token]


def _fingerprint_fields_for(model: type[Model]) -> tuple[str, ...]:
    """
    Return the extra columns DB_PURGE_MCP_FINGERPRINT_FIELDS configures
    for model, to include in its row-set fingerprint alongside the
    primary key.

    Matched case-insensitively against model._meta.label, mirroring
    DB_PURGE_MCP_ALLOWED_MODELS. Empty by default, so fingerprinting is
    PK-only unless a model opts in: not every model has an updated_at
    or version column to hash for in-place mutation detection.
    """
    configured = getattr(settings, "DB_PURGE_MCP_FINGERPRINT_FIELDS", {})
    for label, fields in configured.items():
        if label.lower() == model._meta.label.lower():
            return tuple(fields)
    return ()


def _row_set_snapshot(
    queryset: QuerySet[Any],
    fingerprint_fields: tuple[str, ...],
    limit: int,
) -> tuple[tuple[int, ...], str]:
    """
    Return (pk_list, fingerprint) for up to limit rows of queryset.

    Rows are taken in pk order, so when there are more matches than
    limit the slice is deterministic, and the pk_list and the
    fingerprint hash are built in that same order. No other column
    takes part in the ordering, so a column changing value between
    preview and execute cannot reorder an unchanged set of rows and
    masquerade as row-set drift.
    """
    columns = ("pk", *fingerprint_fields)
    rows = list(queryset.order_by("pk").values_list(*columns)[:limit])
    pk_list = tuple(row[0] for row in rows)
    raw = "|".join(",".join(str(value) for value in row) for row in rows)
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    return pk_list, fingerprint


def issue_token(
    policy_name: str,
    validated: dict[str, ParamValue],
    pk_list: tuple[int, ...],
    fingerprint: str,
    preview_row_count: int,
) -> tuple[str, datetime]:
    """Issue a fresh confirmation token bound to the policy, params and row set."""
    _evict_expired_tokens()
    token = secrets.token_urlsafe(32)
    expires_at = timezone.now() + TOKEN_TTL
    _TOKENS[token] = _TokenRecord(
        params_hash=hash_params(policy_name, validated),
        expires_at=expires_at,
        pk_list=pk_list,
        fingerprint=fingerprint,
        preview_row_count=preview_row_count,
    )
    return token, expires_at


def token_is_valid(
    token: str, policy_name: str, validated: dict[str, ParamValue]
) -> bool:
    """
    Return whether token is currently valid for exactly this policy and params.

    Collapses an unknown token, an expired token, and a token bound to a
    different policy or different params into the same False result.
    Callers must not surface which of these occurred: a single opaque
    failure message prevents an agent (or attacker) from using error
    content to fingerprint token TTLs or probe for near-miss matches.
    """
    record = _TOKENS.get(token)
    if record is None:
        return False
    if timezone.now() >= record.expires_at:
        return False
    return record.params_hash == hash_params(policy_name, validated)


def _pop_valid_token(
    token: str, policy_name: str, validated: dict[str, ParamValue]
) -> _TokenRecord | None:
    """
    Atomically consume token, returning its record only if still valid.

    Pops the token out of the store before checking expiry or params,
    so at most one caller can ever receive a non-None record for a
    given token: this closes the race where two concurrent
    execute_purge calls both observe the token as valid before either
    consumes it. A caller that then fails for a reason that is not its
    fault (for example a row-cap breach) must put the record back with
    _reinstate_token so the token remains usable for a retry.
    """
    record = _TOKENS.pop(token, None)
    if record is None:
        return None
    if timezone.now() >= record.expires_at:
        return None
    if record.params_hash != hash_params(policy_name, validated):
        return None
    return record


def _reinstate_token(token: str, record: _TokenRecord) -> None:
    """Put a popped-but-not-consumed token back, e.g. after a cap breach."""
    _TOKENS[token] = record


def effective_max_rows(policy: Policy) -> int:
    """Return the policy's max_rows if set, else DB_PURGE_MCP_MAX_ROWS."""
    global_cap = get_max_rows_cap()
    return policy.max_rows if policy.max_rows is not None else global_cap


class PurgePreview(TypedDict):
    row_count: int
    sample_rows: list[dict[str, object]]
    confirmation_token: str
    token_expires_at: str


def _json_value(value: object) -> object:
    """Render a column value for the agent as plain JSON data."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _sample_rows(
    policy: Policy, matched: QuerySet[Any], limit: int
) -> list[dict[str, object]]:
    """
    Return up to limit matched rows: the pk and each param's current value.

    Values are keyed by param name, never by field name, so a preview
    does not reveal which columns a policy filters on. A policy with no
    params returns the pk only.
    """
    fields = list(dict.fromkeys(param.field for param in policy.params))
    rows = matched.order_by("pk").values("pk", *fields)[:limit]
    return [
        {
            "pk": row["pk"],
            **{param.name: _json_value(row[param.field]) for param in policy.params},
        }
        for row in rows
    ]


def preview_purge_candidates(
    policy_name: str, params: dict[str, ParamValue] | None = None
) -> PurgePreview:
    """
    Count and sample rows a policy would delete, without deleting them.

    Loads the policy and validates params (validate_request) before any
    query. Returns the total match count, up to PREVIEW_SAMPLE_SIZE
    sample rows (primary key and each param's value only, never the
    full row), and a fresh confirmation token bound to the policy name,
    the validated params, and a fingerprint of the matched row set (see
    _row_set_snapshot), so execute_purge can detect drift instead of
    trusting the parameters alone. A match count above the policy's
    effective row cap is refused here, so no token is issued for a
    purge that execute_purge would reject.
    """
    policy = get_policy(policy_name)
    validated = validate_request(policy, params)
    matched = build_queryset(policy, validated, timezone.now())

    row_count = matched.count()
    max_rows = effective_max_rows(policy)
    if row_count > max_rows:
        raise PurgePolicyError(
            f"Policy would match {row_count} rows, exceeding its max of {max_rows}. "
            "Narrow the params and preview again."
        )
    sample_rows = _sample_rows(policy, matched, PREVIEW_SAMPLE_SIZE)
    pk_list, fingerprint = _row_set_snapshot(
        matched, _fingerprint_fields_for(policy.model), max_rows
    )
    token, expires_at = issue_token(
        policy.name, validated, pk_list, fingerprint, row_count
    )

    logger.info(
        "Previewed purge policy %s params=%s: %s rows matched",
        policy.name,
        validated,
        row_count,
    )

    return {
        "row_count": row_count,
        "sample_rows": sample_rows,
        "confirmation_token": token,
        "token_expires_at": expires_at.isoformat(),
    }


@mcp.tool
def preview_purge(
    policy_name: str,
    params: dict[str, StrictInt | StrictFloat | StrictStr] | None = None,
) -> PurgePreview:
    """
    Preview which rows a purge policy would delete, without deleting them.

    policy_name must be one returned by list_policies. params must hold
    exactly the params that policy declares, each within its bounds or
    from its choices, and may be omitted for a policy that declares
    none. Nothing else can be supplied: the policy, not the caller,
    decides the model, the columns and the filter, and a value outside
    its declared bounds is rejected before the database is touched.
    Returns the matching row count, up to 5 sample rows (primary key
    and each param's value, keyed by param name), a confirmation_token,
    and token_expires_at. A count above the policy's max_rows is
    refused. Pass the token, the same policy_name and identical params
    to execute_purge before it expires (5 minutes) to actually delete
    rows. This tool performs no deletion.
    """
    try:
        return preview_purge_candidates(policy_name, params)
    except PurgePolicyError as exc:
        raise ToolError(str(exc)) from exc


class PurgeResult(TypedDict):
    deleted_count: int
    duration_seconds: float


def execute_purge_candidates(
    policy_name: str,
    confirmation_token: str,
    params: dict[str, ParamValue] | None = None,
) -> PurgeResult:
    """
    Delete rows matched by a prior, still-valid preview_purge call.

    Loads the policy and validates params exactly as preview does, then
    requires confirmation_token to be valid for this policy name and
    these params. The token is popped out of the store before it is
    checked, so at most one caller can ever consume a given token even
    if two execute_purge calls race. Any mismatch, expiry, or unknown
    token raises PurgePolicyError(TOKEN_ERROR_MESSAGE), the same
    message regardless of which of those it was. The token is consumed
    only once the delete actually succeeds: if the call fails for any
    reason first, including a row-cap breach or a row-set fingerprint
    mismatch (see below), the token is put back so a retry with the
    same token can still succeed within its TTL.

    The policy's max_rows (else DB_PURGE_MCP_MAX_ROWS) bounds the number
    of *matching* rows, the same count preview_purge reports, re-checked
    here right before deleting. It does not bound cascade fan-out: ON
    DELETE CASCADE relations can remove additional related rows. The
    deleted_count returned and logged is delete()'s own return value,
    which does include those cascades.

    Deletion targets the exact primary keys preview_purge captured,
    not a re-run of the policy's filter: a row that starts matching
    after preview (a phantom insert) is structurally excluded, since
    it was never in that PK list. Before deleting, this re-runs the
    filter, select and exclude included, and recomputes the row-set
    fingerprint (PKs, plus any DB_PURGE_MCP_FINGERPRINT_FIELDS columns
    configured for the model) to detect drift: PKs that left or joined
    the match set, or (only with fingerprint_fields configured)
    in-place mutation of a still-matching row. A fingerprint mismatch
    raises PurgeRowSetChangedError naming the previewed and current
    row counts, and never retries or reissues a token automatically:
    the caller must run preview_purge again. Where the database
    backend supports SELECT ... FOR UPDATE (connection.features.
    has_select_for_update), the re-check locks the candidate rows for
    the rest of the transaction; on backends that do not (e.g. SQLite)
    this is skipped rather than raised or degraded, since delete-by-PK
    already makes phantom inserts harmless and the lock is a nicety on
    top of that, not the safety mechanism.
    """
    policy = get_policy(policy_name)
    validated = validate_request(policy, params)

    record = _pop_valid_token(confirmation_token, policy.name, validated)
    if record is None:
        raise PurgePolicyError(TOKEN_ERROR_MESSAGE)

    max_rows = effective_max_rows(policy)
    fingerprint_fields = _fingerprint_fields_for(policy.model)

    start_time = time.monotonic()
    try:
        with transaction.atomic():
            matched = build_queryset(policy, validated, timezone.now())
            if connection.features.has_select_for_update:
                matched = matched.select_for_update()

            # count() and the fingerprint re-check below are separate
            # queries from the eventual delete, so the row cap and the
            # fingerprint are both coarse safety nets rather than exact
            # guarantees on backends without row locking.
            row_count = matched.count()
            if row_count > max_rows:
                raise PurgePolicyError(
                    f"Purge would match {row_count} rows, exceeding the max of "
                    f"{max_rows} for this policy. Refusing to execute."
                )

            _, current_fingerprint = _row_set_snapshot(
                matched, fingerprint_fields, max_rows
            )
            if current_fingerprint != record.fingerprint:
                raise PurgeRowSetChangedError(
                    "The matched row set has changed since preview_purge "
                    f"(previewed {record.preview_row_count} row(s), now {row_count}). "
                    "Run preview_purge again and execute with its new token."
                )

            deleted_total, deleted_by_model = policy.model._default_manager.filter(
                pk__in=record.pk_list
            ).delete()
    except Exception:
        _reinstate_token(confirmation_token, record)
        raise
    took = time.monotonic() - start_time

    logger.info(
        "Executed purge policy %s params=%s: deleted %s rows (took %.3fs)",
        policy.name,
        validated,
        deleted_total,
        took,
    )
    logger.debug(
        "Purge delete breakdown for policy %s: %s", policy.name, deleted_by_model
    )

    return {"deleted_count": deleted_total, "duration_seconds": took}


@mcp.tool
def execute_purge(
    policy_name: str,
    confirmation_token: str,
    params: dict[str, StrictInt | StrictFloat | StrictStr] | None = None,
) -> PurgeResult:
    """
    Delete rows matched by a prior, still-valid preview_purge call.

    Pass the same policy_name and identical params that preview_purge
    received, with the confirmation_token it returned, within that
    token's 5 minute lifetime. The policy is loaded and params are
    validated again, and the policy's model must still be in
    DB_PURGE_MCP_ALLOWED_MODELS (empty by default, so nothing is
    purgeable until configured). An unknown token, an expired token,
    and a token issued for a different policy or different params all
    fail with the identical "invalid or expired confirmation token"
    message, by design: callers cannot distinguish why a token was
    rejected. Tokens are single-use: only consumed once this call's
    delete actually succeeds. If the call fails for any other reason (a
    row-cap breach, or a row-set fingerprint mismatch), the token is
    restored so a retry with the same token can still succeed within
    its TTL.

    The policy's max_rows (else DB_PURGE_MCP_MAX_ROWS) is re-checked
    against the live matching row count at execute time, even if
    preview_purge saw a count under the cap. It bounds matching rows,
    not cascade fan-out from related models; the deleted_count returned
    here is the real total, cascades included.

    The token also binds a fingerprint of the exact row set
    preview_purge matched (their primary keys, plus any
    DB_PURGE_MCP_FINGERPRINT_FIELDS columns configured for the model).
    Deletion is by that PK list, not a re-run of the filter, so rows
    that start matching only after preview are never touched. Before
    deleting, the filter is re-run and the fingerprint recomputed; a
    mismatch (rows added, removed, or, with fingerprint_fields
    configured, mutated in place) fails the call with a distinct
    "row set changed" error naming the previewed and current row
    counts, rather than deleting a set the caller never saw.
    """
    try:
        return execute_purge_candidates(policy_name, confirmation_token, params)
    except PurgePolicyError as exc:
        raise ToolError(str(exc)) from exc


def configure_stderr_logging(level: int = logging.INFO) -> None:
    """
    Reset root logging to a single stderr handler.

    stdout carries the MCP stdio JSON-RPC stream, so a log line written
    there is indistinguishable from a protocol message and breaks every
    client reading it. Nothing may log to stdout in this process.
    """
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


class Command(BaseCommand):
    help = "Run the django-db-purge MCP server over stdio"

    def handle(self, *args: Any, **options: Any) -> None:
        if FastMCP is None:
            raise CommandError(MISSING_FASTMCP_ERROR)
        # Fail closed at startup: an invalid policy stops the command here, so
        # the server never starts with a config it would reject on every call.
        try:
            policies = load_policies()
        except ImproperlyConfigured as exc:
            raise CommandError(f"Invalid DB_PURGE_MCP_POLICIES: {exc}") from exc
        configure_stderr_logging()
        logger.info(
            "Starting django-db-purge MCP server over stdio with %s policies",
            len(policies),
        )
        mcp.run(transport="stdio")
