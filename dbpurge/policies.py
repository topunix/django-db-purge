"""
Shared guards for the MCP purge server.

This module has no fastmcp import, so it works without the [mcp] extra.
"""

from django.conf import settings
from django.db.models import Model

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
