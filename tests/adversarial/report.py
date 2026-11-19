"""
Render adversarial results into the generated region of evals/README.md.

Usage: python -m tests.adversarial.report RESULTS_JSON [--check]

Rewrites the text between the start and end markers with a results table
and a findings table. With --check, nothing is written and the exit code
is 1 when the file would change.
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Collection
from pathlib import Path
from typing import Any

from tests.adversarial.registry import CASES

START = "<!-- adversarial-results:start -->"
END = "<!-- adversarial-results:end -->"
DEFAULT_README = Path(__file__).resolve().parents[2] / "evals" / "README.md"


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    lines.extend("| " + " | ".join(_cell(item) for item in row) + " |" for row in rows)
    return lines


def render(results: list[dict[str, Any]]) -> str:
    """Return the markdown for the generated region, ending in a newline."""
    ordered = sorted(results, key=lambda record: record["id"])
    lines = _table(
        ["ID", "Category", "Attack", "Expected", "Observed", "Result"],
        [
            [
                r["id"],
                r["category"],
                r["title"],
                r["expected"],
                r["observed"],
                "PASS" if r["passed"] else "FAIL",
            ]
            for r in ordered
        ],
    )
    findings = [r for r in ordered if r["observed"] == "not_blocked"]
    lines.append("")
    if findings:
        lines.extend(
            _table(
                ["ID", "Severity", "Finding", "Proposed fix"],
                [
                    [r["id"], r["severity"], r["title"], r["proposed_fix"]]
                    for r in findings
                ],
            )
        )
    else:
        lines.append("No findings.")
    return "\n".join(lines) + "\n"


def replace_region(text: str, region: str) -> str:
    """Return text with the marked region replaced by region."""
    start = text.index(START) + len(START)
    end = text.index(END)
    return text[:start] + "\n" + region + text[end:]


def main(argv: list[str] | None = None, case_ids: Collection[str] | None = None) -> int:
    """
    Run the report. Exit 2 when the results do not cover exactly the case ids,
    or when two records share an id.

    case_ids defaults to the registry, so a case whose attack crashed or
    was never run cannot pass the staleness check silently.
    """
    parser = argparse.ArgumentParser(prog="python -m tests.adversarial.report")
    parser.add_argument("results", help="JSON written by --adversarial-json")
    parser.add_argument("--check", action="store_true", help="fail if stale")
    parser.add_argument("--readme", default=str(DEFAULT_README), help="README path")
    args = parser.parse_args(argv)

    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    wanted = {case.id for case in CASES} if case_ids is None else set(case_ids)
    counts = Counter(record["id"] for record in results)
    duplicated = sorted(case_id for case_id, count in counts.items() if count > 1)
    if duplicated:
        print(f"duplicate result ids {duplicated}", file=sys.stderr)
        return 2
    found = set(counts)
    if found != wanted:
        print(
            f"results and registry differ, missing {sorted(wanted - found)}, "
            f"unexpected {sorted(found - wanted)}",
            file=sys.stderr,
        )
        return 2
    readme = Path(args.readme)
    current = readme.read_text(encoding="utf-8")
    updated = replace_region(current, render(results))
    if updated == current:
        return 0
    if args.check:
        print(f"{readme} is stale, run the report without --check", file=sys.stderr)
        return 1
    readme.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
