"""Pytest options and the JSON results writer for the adversarial suite."""

import json
from pathlib import Path
from typing import Any

import pytest

RESULTS_KEY = pytest.StashKey[list[dict[str, Any]]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--adversarial-json",
        metavar="PATH",
        default=None,
        help="Write one JSON record per adversarial case to PATH.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.stash[RESULTS_KEY] = []


@pytest.fixture
def record_result(request: pytest.FixtureRequest) -> Any:
    """Return a function that stores one case record for the JSON report."""
    records = request.config.stash[RESULTS_KEY]
    return records.append


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Write the records sorted by id, with no timestamps or durations."""
    path = session.config.getoption("--adversarial-json")
    if not path:
        return
    records = sorted(session.config.stash[RESULTS_KEY], key=lambda r: r["id"])
    text = json.dumps(records, indent=2, sort_keys=True) + "\n"
    Path(path).write_text(text, encoding="utf-8")
