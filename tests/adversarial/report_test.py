"""Tests for the report's handling of malformed result files."""

import json
from pathlib import Path

import pytest

from tests.adversarial import report

pytestmark = pytest.mark.adversarial


def test_report_rejects_duplicate_result_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = {
        "id": "a-blocked",
        "category": "token_lifecycle",
        "title": "A blocked attack",
        "expected": "blocked",
        "observed": "blocked",
        "passed": True,
    }
    results = tmp_path / "results.json"
    results.write_text(json.dumps([record, record]), encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(f"{report.START}\n{report.END}\n", encoding="utf-8")
    before = readme.read_text(encoding="utf-8")

    code = report.main([str(results), "--readme", str(readme)], {"a-blocked"})

    assert code == 2
    assert "a-blocked" in capsys.readouterr().err
    assert readme.read_text(encoding="utf-8") == before
