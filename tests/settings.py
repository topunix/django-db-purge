import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

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
DB_PURGE_MCP_ALLOWED_MODELS = [
    "tests.SampleRecord",
    "tests.PolicyRecord",
    "tests.PolicyNote",
]

# Policies the agent may select by name. One temporal policy over the toy
# model that the tests and the stdio e2e driver purge.
DB_PURGE_MCP_POLICIES = {
    "stale_samples": {
        "model": "tests.SampleRecord",
        "description": "Sample records older than the given age.",
        "params": [
            {
                "name": "older_than_seconds",
                "type": "age_seconds",
                "field": "created_at",
                "min": 1,
                "max": 315360000,
            },
        ],
    },
}
