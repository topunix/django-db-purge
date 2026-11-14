from tests.settings import *

# File-backed sqlite, gitignored, so a demo database persists across
# separate `seed_demo.py` and `purge_mcp_server` process runs, unlike
# the in-memory default in tests.settings.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(REPO_ROOT / "demo.sqlite3"),
    }
}

# One temporal and one non-temporal policy, so a demo shows both kinds. The
# temporal one is inherited from tests.settings (stale_samples).
DB_PURGE_MCP_POLICIES = {
    **DB_PURGE_MCP_POLICIES,
    "demo_labelled_samples": {
        "model": "tests.SampleRecord",
        "description": "Sample records with one of the demo labels.",
        "params": [
            {
                "name": "label",
                "type": "choice",
                "field": "label",
                "choices": ["demo-1d", "demo-12d", "demo-23d"],
            },
        ],
    },
}
