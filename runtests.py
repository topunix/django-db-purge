#!/usr/bin/env python
import importlib.util
import sys
from pathlib import Path

import django
from django.conf import settings
from django.test.utils import get_runner

REPO_ROOT = Path(__file__).resolve().parent


def _register_dbpurge_package() -> None:
    """
    Make this checkout importable as `dbpurge`.

    The package is developed in a directory named django-db-purge, but
    the installed package (and every import in this codebase) is named
    `dbpurge`. Register it under that name so tests can run straight
    from a checkout, with no install step required.
    """
    spec = importlib.util.spec_from_file_location(
        "dbpurge",
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["dbpurge"] = module
    spec.loader.exec_module(module)


def run():
    _register_dbpurge_package()
    settings.configure(
        INSTALLED_APPS=[
            "dbpurge",
            "tests",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        USE_TZ=True,
    )
    django.setup()
    test_runner_class = get_runner(settings)
    failures = test_runner_class().run_tests(["tests"])
    sys.exit(bool(failures))


if __name__ == "__main__":
    run()
