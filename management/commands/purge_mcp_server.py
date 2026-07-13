import logging
import sys

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db.models import DateField, DateTimeField
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("django-db-purge")


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
