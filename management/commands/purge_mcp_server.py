import hashlib
import logging
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypedDict

from django.apps import apps
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import DateField, DateTimeField, Model
from django.utils import timezone
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

mcp = FastMCP("django-db-purge")

TOKEN_TTL = timedelta(minutes=5)
PREVIEW_SAMPLE_SIZE = 5
DEFAULT_MAX_ROWS = 10000
TOKEN_ERROR_MESSAGE = "invalid or expired confirmation token"


class PurgePolicyError(Exception):
    """Raised when preview/execute parameters fail validation."""


@dataclass(frozen=True)
class _TokenRecord:
    params_hash: str
    expires_at: datetime


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
    allowed = getattr(settings, "DB_PURGE_MCP_ALLOWED_MODELS", [])
    return model._meta.label in allowed


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


def issue_token(
    app_name: str, model_name: str, time_column: str, retention_seconds: int
) -> tuple[str, datetime]:
    """Issue a fresh confirmation token bound to the exact parameter set."""
    token = secrets.token_urlsafe(32)
    expires_at = timezone.now() + TOKEN_TTL
    _TOKENS[token] = _TokenRecord(
        params_hash=_hash_params(app_name, model_name, time_column, retention_seconds),
        expires_at=expires_at,
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


def consume_token(token: str) -> None:
    """Invalidate token so it cannot be used again. Tokens are single-use."""
    _TOKENS.pop(token, None)


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
    confirmation token bound to these exact parameters.
    """
    model = validate_policy(app_name, model_name, time_column, retention_seconds)
    cutoff = timezone.now() - timedelta(seconds=retention_seconds)
    expired_records = model.objects.filter(**{f"{time_column}__lte": cutoff})

    row_count = expired_records.count()
    sample_rows = [
        {"pk": obj.pk, time_column: getattr(obj, time_column).isoformat()}
        for obj in expired_records.order_by(time_column)[:PREVIEW_SAMPLE_SIZE]
    ]

    token, expires_at = issue_token(app_name, model_name, time_column, retention_seconds)

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
    introspection. Returns the matching row count, up to 5 sample rows
    (primary key and time_column value only), a confirmation_token, and
    token_expires_at. Pass the token and identical parameters to
    execute_purge before it expires (5 minutes) to actually delete
    rows. This tool performs no deletion.
    """
    try:
        return preview_purge_candidates(app_name, model_name, time_column, retention_seconds)
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

    Requires confirmation_token to be valid for these exact parameters
    (see token_is_valid). Any mismatch, expiry, or unknown token raises
    PurgePolicyError(TOKEN_ERROR_MESSAGE), the same message regardless
    of which of those it was, and leaves the token unconsumed. On
    success the token is consumed (single-use): a second execute call
    with the same token fails the same way a bad token would.

    DB_PURGE_MCP_MAX_ROWS bounds the number of *matching* rows (the
    same count preview_purge reports and what is re-checked here right
    before deleting), not cascade fan-out: ON DELETE CASCADE relations
    can remove additional related rows this cap does not account for.
    The deleted_count returned and logged is delete()'s own return
    value, which does include those cascades.
    """
    model = validate_policy(app_name, model_name, time_column, retention_seconds)

    if not token_is_valid(
        confirmation_token, app_name, model_name, time_column, retention_seconds
    ):
        raise PurgePolicyError(TOKEN_ERROR_MESSAGE)

    max_rows = getattr(settings, "DB_PURGE_MCP_MAX_ROWS", DEFAULT_MAX_ROWS)
    cutoff = timezone.now() - timedelta(seconds=retention_seconds)

    start_time = time.time()
    with transaction.atomic():
        expired_records = model.objects.filter(**{f"{time_column}__lte": cutoff})
        # count() and delete() are two separate queries. A concurrent
        # writer can still insert or update a row matching this filter
        # between them, even inside this transaction, so the row cap
        # below is a coarse safety net rather than an exact guarantee.
        row_count = expired_records.count()
        if row_count > max_rows:
            raise PurgePolicyError(
                f"Purge would match {row_count} rows, exceeding the configured "
                f"max of {max_rows} (DB_PURGE_MCP_MAX_ROWS). Refusing to execute."
            )
        deleted_total, deleted_by_model = expired_records.delete()
    took = time.time() - start_time

    consume_token(confirmation_token)

    logger.info(
        "Executed purge of %s %s: deleted %s rows older than %s (took %.3fs)",
        app_name,
        model_name,
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

    Requires an exact parameter match against a preview_purge call,
    using the confirmation_token it returned, within that token's 5
    minute lifetime. An unknown token, an expired token, and a token
    whose parameters no longer match all fail with the identical
    "invalid or expired confirmation token" message, by design:
    callers cannot distinguish why a token was rejected. Tokens are
    single-use, consumed only on a successful delete.

    DB_PURGE_MCP_MAX_ROWS is re-checked against the live matching row
    count at execute time, even if preview_purge saw a count under the
    cap. It bounds matching rows, not cascade fan-out from related
    models; the deleted_count returned here is the real total,
    cascades included.
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
        configure_stderr_logging()
        logger.info("Starting django-db-purge MCP server over stdio")
        mcp.run(transport="stdio")
