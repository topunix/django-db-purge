from tests.settings import *  # noqa: F401,F403

# File-backed sqlite, gitignored, so a demo database persists across
# separate `seed_demo.py` and `purge_mcp_server` process runs, unlike
# the in-memory default in tests.settings.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(REPO_ROOT / "demo.sqlite3"),
    }
}
