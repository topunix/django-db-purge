import hashlib
import logging
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

try:
    from typing_extensions import TypedDict
except ImportError:
    from typing import TypedDict

from django.apps import apps
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import DateField, DateTimeField, Model, QuerySet
from django.utils import timezone

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


class _MissingMCP:
    """Stand-in for FastMCP when fastmcp is absent, so this module stays importable."""

    def tool(self, fn):
        return fn


mcp = FastMCP("django-db-purge") if FastMCP is not None else _MissingMCP()

TOKEN_TTL = timedelta(minutes=5)
PREVIEW_SAMPLE_SIZE = 5
DEFAULT_MAX_ROWS = 10000
TOKEN_ERROR_MESSAGE = "invalid or expired confirmation token"


class PurgePolicyError(Exception):
    """Raised when preview/execute parameters fail validation."""


class PurgeRowSetChangedError(PurgePolicyError):
    """Raised when the matched row set differs between preview and execute."""


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


def get_purge_candidates() -> dict[str, dict[str, list[str]]]:
    """
    Return every installed model's date and datetime columns.

    Read-only. Maps app label to model name to the list of field names
    on that model with type DateField or DateTimeField, since those are
    the only columns a retention policy can filter on.
    """
    candidates: dict[str, dict[str, list[str]]] = {}
    for model in apps.get_models():
        date_columns = [
            field.name
            for field in model._meta.get_fields()
            if isinstance(field, (DateField, DateTimeField))
        ]
        if not date_columns:
            continue
        app_label = model._meta.app_label
        model_name = model._meta.model_name
        candidates.setdefault(app_label, {})[model_name] = date_columns
    return candidates


@mcp.tool
def list_purge_candidates() -> dict[str, dict[str, list[str]]]:
    """
    List installed apps, models, and their date or datetime columns.

    Read-only. Use the returned (app_name, model_name, time_column)
    combinations as valid inputs to preview_purge.
    """
    return get_purge_candidates()


def _is_allowed_model(model: type[Model]) -> bool:
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


def validate_policy(
    app_name: str,
    model_name: str,
    time_column: str,
    retention_seconds: int,
) -> type[Model]:
    """
    Validate a purge policy against live schema and return the model.

    Shared guard for preview_purge and execute_purge. Raises
    PurgePolicyError if the model does not exist, is not in the
    DB_PURGE_MCP_ALLOWED_MODELS allowlist (empty by default, meaning
    nothing is purgeable until configured), time_column is not a real
    DateField/DateTimeField on it, or retention_seconds is not a
    positive integer.
    """
    try:
        model = apps.get_model(app_label=app_name, model_name=model_name)
    except LookupError:
        raise PurgePolicyError(f"Model '{model_name}' in app '{app_name}' not found")

    if not _is_allowed_model(model):
        raise PurgePolicyError(
            f"Model '{model._meta.label}' is not in DB_PURGE_MCP_ALLOWED_MODELS "
            "and cannot be purged via MCP"
        )

    try:
        field = model._meta.get_field(time_column)
    except FieldDoesNotExist:
        raise PurgePolicyError(
            f"Column '{time_column}' does not exist on model '{model_name}'"
        )
    if not isinstance(field, (DateField, DateTimeField)):
        raise PurgePolicyError(
            f"Column '{time_column}' on model '{model_name}' is not a date or "
            "datetime column"
        )

    if not isinstance(retention_seconds, int) or isinstance(retention_seconds, bool):
        raise PurgePolicyError("retention_seconds must be a positive integer")
    if retention_seconds <= 0:
        raise PurgePolicyError("retention_seconds must be a positive integer")

    return model


def _hash_params(
    app_name: str, model_name: str, time_column: str, retention_seconds: int
) -> str:
    raw = "|".join([app_name, model_name, time_column, str(retention_seconds)])
    return hashlib.sha256(raw.encode()).hexdigest()


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
    queryset: QuerySet,
    time_column: str,
    fingerprint_fields: tuple[str, ...],
    limit: int,
) -> tuple[tuple[int, ...], str]:
    """
    Return (pk_list, fingerprint) for up to limit rows of queryset.

    Rows are selected in (time_column, pk) order so that, when there
    are more matches than limit, the slice taken is deterministic.
    The selected rows are then sorted by pk alone before the pk_list
    and the fingerprint hash are built: time_column (and any other
    non-fingerprinted column) can change value between preview and
    execute without reordering an otherwise-unchanged set of rows, and
    an ordering artifact must not masquerade as row-set drift.
    """
    columns = ("pk", *fingerprint_fields)
    rows = list(queryset.order_by(time_column, "pk").values_list(*columns)[:limit])
    rows.sort(key=lambda row: row[0])
    pk_list = tuple(row[0] for row in rows)
    raw = "|".join(",".join(str(value) for value in row) for row in rows)
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    return pk_list, fingerprint


def issue_token(
    app_name: str,
    model_name: str,
    time_column: str,
    retention_seconds: int,
    pk_list: tuple[int, ...],
    fingerprint: str,
    preview_row_count: int,
) -> tuple[str, datetime]:
    """Issue a fresh confirmation token bound to the parameters and row set."""
    _evict_expired_tokens()
    token = secrets.token_urlsafe(32)
    expires_at = timezone.now() + TOKEN_TTL
    _TOKENS[token] = _TokenRecord(
        params_hash=_hash_params(app_name, model_name, time_column, retention_seconds),
        expires_at=expires_at,
        pk_list=pk_list,
        fingerprint=fingerprint,
        preview_row_count=preview_row_count,
    )
    return token, expires_at


def token_is_valid(
    token: str,
    app_name: str,
    model_name: str,
    time_column: str,
    retention_seconds: int,
) -> bool:
    """
    Return whether token is currently valid for exactly these parameters.

    Collapses an unknown token, an expired token, and a token whose
    parameters do not match into the same False result. Callers must
    not surface which of these occurred: a single opaque failure
    message prevents an agent (or attacker) from using error content
    to fingerprint token TTLs or probe for near-miss parameter matches.
    """
    record = _TOKENS.get(token)
    if record is None:
        return False
    if timezone.now() >= record.expires_at:
        return False
    if record.params_hash != _hash_params(
        app_name, model_name, time_column, retention_seconds
    ):
        return False
    return True


def _pop_valid_token(
    token: str,
    app_name: str,
    model_name: str,
    time_column: str,
    retention_seconds: int,
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
    if record.params_hash != _hash_params(
        app_name, model_name, time_column, retention_seconds
    ):
        return None
    return record


def _reinstate_token(token: str, record: _TokenRecord) -> None:
    """Put a popped-but-not-consumed token back, e.g. after a cap breach."""
    _TOKENS[token] = record


def _expired_queryset(
    model: type[Model], time_column: str, retention_seconds: int
) -> tuple[QuerySet, datetime]:
    """
    Return (rows older than retention_seconds, the cutoff used).

    Shared by preview_purge_candidates and execute_purge_candidates so
    the two can never silently disagree about which rows are expired.
    """
    cutoff = timezone.now() - timedelta(seconds=retention_seconds)
    return model.objects.filter(**{f"{time_column}__lte": cutoff}), cutoff


class PurgePreview(TypedDict):
    row_count: int
    sample_rows: list[dict[str, object]]
    confirmation_token: str
    token_expires_at: str


def preview_purge_candidates(
    app_name: str,
    model_name: str,
    time_column: str,
    retention_seconds: int,
) -> PurgePreview:
    """
    Count and sample rows a purge would delete, without deleting them.

    Validates the policy via validate_policy, then filters model rows
    where time_column is older than retention_seconds. Returns the
    total match count, up to PREVIEW_SAMPLE_SIZE sample rows (primary
    key and time_column value only, never the full row), and a fresh
    confirmation token bound to these exact parameters and to a
    fingerprint of the matched row set (see _row_set_snapshot), so
    execute_purge can detect drift instead of trusting the parameters
    alone.
    """
    model = validate_policy(app_name, model_name, time_column, retention_seconds)
    expired_records, cutoff = _expired_queryset(model, time_column, retention_seconds)

    row_count = expired_records.count()
    sample_rows = [
        {"pk": obj.pk, time_column: getattr(obj, time_column).isoformat()}
        for obj in expired_records.order_by(time_column)[:PREVIEW_SAMPLE_SIZE]
    ]

    max_rows = getattr(settings, "DB_PURGE_MCP_MAX_ROWS", DEFAULT_MAX_ROWS)
    fingerprint_fields = _fingerprint_fields_for(model)
    pk_list, fingerprint = _row_set_snapshot(
        expired_records, time_column, fingerprint_fields, max_rows
    )

    token, expires_at = issue_token(
        app_name,
        model_name,
        time_column,
        retention_seconds,
        pk_list,
        fingerprint,
        row_count,
    )

    logger.info(
        "Previewed purge of %s %s: %s rows older than %s",
        app_name,
        model_name,
        row_count,
        cutoff,
    )

    return {
        "row_count": row_count,
        "sample_rows": sample_rows,
        "confirmation_token": token,
        "token_expires_at": expires_at.isoformat(),
    }


@mcp.tool
def preview_purge(
    app_name: str,
    model_name: str,
    time_column: str,
    retention_seconds: int,
) -> PurgePreview:
    """
    Preview which rows a purge would delete, without deleting them.

    Validates app_name, model_name, and time_column against live model
    introspection, and requires the model to be listed in
    DB_PURGE_MCP_ALLOWED_MODELS (empty by default, so nothing is
    purgeable until configured). Returns the matching row count, up to
    5 sample rows (primary key and time_column value only), a
    confirmation_token, and token_expires_at. Pass the token and
    identical parameters to execute_purge before it expires (5 minutes)
    to actually delete rows. This tool performs no deletion.
    """
    try:
        return preview_purge_candidates(
            app_name, model_name, time_column, retention_seconds
        )
    except PurgePolicyError as exc:
        raise ToolError(str(exc)) from exc


class PurgeResult(TypedDict):
    deleted_count: int
    duration_seconds: float


def execute_purge_candidates(
    app_name: str,
    model_name: str,
    time_column: str,
    retention_seconds: int,
    confirmation_token: str,
) -> PurgeResult:
    """
    Delete rows matched by a prior, still-valid preview_purge call.

    Requires confirmation_token to be valid for these exact parameters.
    The token is popped out of the store before it is checked, so at
    most one caller can ever consume a given token even if two
    execute_purge calls race. Any mismatch, expiry, or unknown token
    raises PurgePolicyError(TOKEN_ERROR_MESSAGE), the same message
    regardless of which of those it was. The token is consumed only
    once the delete actually succeeds: if the call fails for any
    reason first, including a row-cap breach or a row-set fingerprint
    mismatch (see below), the token is put back so a retry with the
    same token can still succeed within its TTL.

    DB_PURGE_MCP_MAX_ROWS bounds the number of *matching* rows (the
    same count preview_purge reports and what is re-checked here right
    before deleting), not cascade fan-out: ON DELETE CASCADE relations
    can remove additional related rows this cap does not account for.
    The deleted_count returned and logged is delete()'s own return
    value, which does include those cascades.

    Deletion targets the exact primary keys preview_purge captured,
    not a re-run of the retention filter: a row that starts matching
    the filter after preview (a phantom insert) is structurally
    excluded, since it was never in that PK list. Before deleting,
    this re-runs the filter and recomputes the row-set fingerprint
    (PKs, plus any DB_PURGE_MCP_FINGERPRINT_FIELDS columns configured
    for the model) to detect drift, PKs that left or joined the match
    set, or (only with fingerprint_fields configured) in-place
    mutation of a still-matching row. A fingerprint mismatch raises
    PurgeRowSetChangedError naming the previewed and current row
    counts, and never retries or reissues a token automatically:
    the caller must run preview_purge again. Where the database
    backend supports SELECT ... FOR UPDATE (connection.features.
    has_select_for_update), the re-check locks the candidate rows for
    the rest of the transaction; on backends that do not (e.g. SQLite)
    this is skipped rather than raised or degraded, since delete-by-PK
    already makes phantom inserts harmless and the lock is a nicety on
    top of that, not the safety mechanism.
    """
    model = validate_policy(app_name, model_name, time_column, retention_seconds)

    record = _pop_valid_token(
        confirmation_token, app_name, model_name, time_column, retention_seconds
    )
    if record is None:
        raise PurgePolicyError(TOKEN_ERROR_MESSAGE)

    max_rows = getattr(settings, "DB_PURGE_MCP_MAX_ROWS", DEFAULT_MAX_ROWS)
    fingerprint_fields = _fingerprint_fields_for(model)

    start_time = time.monotonic()
    try:
        with transaction.atomic():
            expired_records, cutoff = _expired_queryset(
                model, time_column, retention_seconds
            )
            if connection.features.has_select_for_update:
                expired_records = expired_records.select_for_update()

            # count() and the fingerprint re-check below are separate
            # queries from the eventual delete, so the row cap and the
            # fingerprint are both coarse safety nets rather than exact
            # guarantees on backends without row locking.
            row_count = expired_records.count()
            if row_count > max_rows:
                raise PurgePolicyError(
                    f"Purge would match {row_count} rows, exceeding the configured "
                    f"max of {max_rows} (DB_PURGE_MCP_MAX_ROWS). Refusing to execute."
                )

            _, current_fingerprint = _row_set_snapshot(
                expired_records, time_column, fingerprint_fields, max_rows
            )
            if current_fingerprint != record.fingerprint:
                raise PurgeRowSetChangedError(
                    "The matched row set has changed since preview_purge "
                    f"(previewed {record.preview_row_count} row(s), now {row_count}). "
                    "Run preview_purge again and execute with its new token."
                )

            deleted_total, deleted_by_model = model.objects.filter(
                pk__in=record.pk_list
            ).delete()
    except Exception:
        _reinstate_token(confirmation_token, record)
        raise
    took = time.monotonic() - start_time

    logger.info(
        "Executed purge of %s.%s.%s (retention=%ss): deleted %s rows older than %s "
        "(took %.3fs)",
        app_name,
        model_name,
        time_column,
        retention_seconds,
        deleted_total,
        cutoff,
        took,
    )
    logger.debug(
        "Purge delete breakdown for %s %s: %s", app_name, model_name, deleted_by_model
    )

    return {"deleted_count": deleted_total, "duration_seconds": took}


@mcp.tool
def execute_purge(
    app_name: str,
    model_name: str,
    time_column: str,
    retention_seconds: int,
    confirmation_token: str,
) -> PurgeResult:
    """
    Delete rows matched by a prior, still-valid preview_purge call.

    Requires the model to be listed in DB_PURGE_MCP_ALLOWED_MODELS
    (empty by default, so nothing is purgeable until configured), the
    same check preview_purge already applied, re-verified here. Also
    requires an exact parameter match against a preview_purge call,
    using the confirmation_token it returned, within that token's 5
    minute lifetime. An unknown token, an expired token, and a token
    whose parameters no longer match all fail with the identical
    "invalid or expired confirmation token" message, by design:
    callers cannot distinguish why a token was rejected. Tokens are
    single-use: only consumed once this call's delete actually
    succeeds. If the call fails for any other reason (a row-cap
    breach, or a row-set fingerprint mismatch), the token is restored
    so a retry with the same token can still succeed within its TTL.

    DB_PURGE_MCP_MAX_ROWS is re-checked against the live matching row
    count at execute time, even if preview_purge saw a count under the
    cap. It bounds matching rows, not cascade fan-out from related
    models; the deleted_count returned here is the real total,
    cascades included.

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
        return execute_purge_candidates(
            app_name, model_name, time_column, retention_seconds, confirmation_token
        )
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

    def handle(self, *args, **options):
        if FastMCP is None:
            raise CommandError(MISSING_FASTMCP_ERROR)
        configure_stderr_logging()
        logger.info("Starting django-db-purge MCP server over stdio")
        mcp.run(transport="stdio")
