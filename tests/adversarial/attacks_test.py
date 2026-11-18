"""
Pytest entry point for the adversarial suite.

One test per registry case. A case declared "blocked" must be blocked, and
a case declared "finding" must still be open, so a stale registry fails
just as a regression does. The fix for either is a registry change made
by a human, never a change under dbpurge/.
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from tests.adversarial import report
from tests.adversarial.attacks import ATTACKS, run_attack
from tests.adversarial.registry import CASES, CATEGORIES, SEVERITIES, Case

pytestmark = pytest.mark.adversarial


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_attack(case: Case, db: None, record_result: Any) -> None:
    outcome = run_attack(case.id)
    want = "blocked" if case.expected == "blocked" else "not_blocked"
    passed = outcome.observed == want
    record_result(
        {
            "id": case.id,
            "category": case.category,
            "title": case.title,
            "expected": case.expected,
            "observed": outcome.observed,
            "passed": passed,
            "evidence": outcome.evidence,
            "severity": case.severity,
            "proposed_fix": case.proposed_fix,
        }
    )
    if case.expected == "blocked":
        assert passed, f"not blocked: {outcome.evidence}"
    else:
        assert passed, f"registry is stale, the gap is now closed: {outcome.evidence}"


def test_registry_and_attacks_have_the_same_ids() -> None:
    assert {case.id for case in CASES} == set(ATTACKS)


def test_registry_shape() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))
    for category in CATEGORIES:
        in_category = [case for case in CASES if case.category == category]
        assert len(in_category) >= 3, category
    assert {case.category for case in CASES} == set(CATEGORIES)
    for case in CASES:
        if case.expected == "finding":
            assert case.severity in SEVERITIES, case.id
            assert case.proposed_fix and case.proposed_fix.strip(), case.id
        else:
            assert case.expected == "blocked", case.id
            assert case.severity is None and case.proposed_fix is None, case.id
    assert "cap-aggregate-bypass" in {
        case.id for case in CASES if case.category == "row_cap"
    }


SAMPLE_RESULTS = [
    {
        "id": "a-blocked",
        "category": "token_lifecycle",
        "title": "A blocked attack",
        "expected": "blocked",
        "observed": "blocked",
        "passed": True,
        "evidence": "ignored",
        "severity": None,
        "proposed_fix": None,
    },
    {
        "id": "b-open",
        "category": "row_cap",
        "title": "An open | gap",
        "expected": "finding",
        "observed": "not_blocked",
        "passed": True,
        "evidence": "ignored",
        "severity": "medium",
        "proposed_fix": "Add a budget.",
    },
]

EXPECTED_REGION = """\
| ID | Category | Attack | Expected | Observed | Result |
| --- | --- | --- | --- | --- | --- |
| a-blocked | token_lifecycle | A blocked attack | blocked | blocked | PASS |
| b-open | row_cap | An open \\| gap | finding | not_blocked | PASS |

| ID | Severity | Finding | Proposed fix |
| --- | --- | --- | --- |
| b-open | medium | An open \\| gap | Add a budget. |
"""


def test_report_renders_the_expected_markdown() -> None:
    assert report.render(SAMPLE_RESULTS) == EXPECTED_REGION


def test_report_check_detects_a_stale_region(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    results.write_text(json.dumps(SAMPLE_RESULTS), encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(
        f"intro\n{report.START}\nold\n{report.END}\noutro\n", encoding="utf-8"
    )
    stale = readme.read_text(encoding="utf-8")

    assert report.main([str(results), "--check", "--readme", str(readme)]) == 1
    assert readme.read_text(encoding="utf-8") == stale

    assert report.main([str(results), "--readme", str(readme)]) == 0
    assert EXPECTED_REGION in readme.read_text(encoding="utf-8")
    assert report.main([str(results), "--check", "--readme", str(readme)]) == 0


def test_registry_cases_serialize() -> None:
    assert all(asdict(case)["id"] for case in CASES)
