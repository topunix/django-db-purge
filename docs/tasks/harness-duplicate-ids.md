# harness-duplicate-ids

## Goal

The adversarial report fails when two result records share an id.

## Scope

In: the id check in `tests/adversarial/report.py`.

Out: any change under `dbpurge/`. Any change to attack cases.

## Files touched

- `tests/adversarial/report.py`
- `tests/adversarial/report_test.py` (new)

## Acceptance criteria

1. A duplicate id fails the run with a message naming the id.
2. The full suite passes.
3. Regenerating `evals/README.md` produces no diff.

## Tests to add

One test that feeds two records with the same id and asserts the failure.

## Docs to update

None.

## Roadmap item addressed

None (minor finding from PR #20 review).
