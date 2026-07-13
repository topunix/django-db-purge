#!/usr/bin/env python
"""
Seed demo.sqlite3 for manually trying the MCP server against real data.

Run: python tests/seed_demo.py
Then: DJANGO_SETTINGS_MODULE=tests.demo_settings python manage.py purge_mcp_server
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.demo_settings")

import django

django.setup()

from django.core.management import call_command
from django.utils import timezone

from tests.models import SampleRecord

AGES_IN_DAYS = [1 + i * 11 for i in range(10)]  # 1, 12, 23, ..., 100


def seed() -> None:
    call_command("migrate", run_syncdb=True, verbosity=0)

    # Idempotent: start from a clean slate so re-running gives the same
    # demo data instead of accumulating rows across runs.
    SampleRecord.objects.all().delete()

    now = timezone.now()
    for age in AGES_IN_DAYS:
        SampleRecord.objects.create(
            created_at=now - timedelta(days=age), label=f"demo-{age}d"
        )

    print(f"Seeded {len(AGES_IN_DAYS)} SampleRecord rows into demo.sqlite3")


if __name__ == "__main__":
    seed()
