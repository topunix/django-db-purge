import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _register_dbpurge_package() -> None:
    """
    Make this checkout importable as `dbpurge`.

    The package is developed in a directory named django-db-purge, but
    the installed package (and every import in this codebase) is named
    `dbpurge`. Register it under that name so tests and the stdio e2e
    driver can run straight from a checkout, with no install step
    required.
    """
    if "dbpurge" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "dbpurge",
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["dbpurge"] = module
    spec.loader.exec_module(module)


_register_dbpurge_package()

SECRET_KEY = "test"
INSTALLED_APPS = [
    "dbpurge",
    "tests",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        # In-memory for the unit test suite. The stdio e2e driver
        # overrides this to a file path via DBPURGE_TEST_DB so the
        # seeding step and the subprocess server can see the same
        # database.
        "NAME": os.environ.get("DBPURGE_TEST_DB", ":memory:"),
    }
}
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Purgeable by default in tests only. Real deployments default to an
# empty allowlist (see purge_mcp_server.py), so nothing is purgeable
# until explicitly configured.
DB_PURGE_MCP_ALLOWED_MODELS = ["tests.SampleRecord"]
