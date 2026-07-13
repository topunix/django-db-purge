#!/usr/bin/env python
import os
import sys

import django
from django.conf import settings
from django.test.utils import get_runner


def run():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
    django.setup()
    test_runner_class = get_runner(settings)
    failures = test_runner_class().run_tests(["tests"])
    sys.exit(bool(failures))


if __name__ == "__main__":
    run()
