# adversarial-harness

## Goal

Red-team the MCP surface (list_policies, preview_purge, execute_purge) with a
declarative registry of attack cases and a hermetic runner. Every case either
proves the server blocks the attack or records a finding with a severity and a
proposed fix. The results are emitted as JUnit and JSON, and a generated
markdown results table, next to a written threat model, lives in
`evals/README.md`. A CI job runs the suite and fails if the committed table is
stale.

## Scope

### In

- `tests/adversarial/registry.py`: the declarative attack registry. Plain data,
  stdlib imports only (no pytest, no fastmcp, no Django). Each case has:
  - `id`: unique, stable, kebab-case, prefixed by its category (for example
    `token-forged`, `binding-extra-param`).
  - `category`: one of exactly six values: `token_lifecycle`,
    `parameter_binding`, `row_set_fingerprint`, `allowlist_scope_escape`,
    `row_cap`, `protocol_injection`.
  - `title` (one line) and `threat` (what an attacker is trying to achieve).
  - `expected`: `blocked` or `finding`.
  - `severity` and `proposed_fix`: required (non-empty) when `expected` is
    `finding`, `None` otherwise. Severity is one of `low`, `medium`, `high`,
    `critical`.
  - At least three cases per category.
- The first commit of the implementation (the first commit after the commit
  that adds this spec) contains `tests/adversarial/registry.py` and nothing
  else. The maintainer revises the registry in PR review. In that first commit
  every case is `expected="blocked"` except the suspected gap below, which is
  `expected="finding"`.
- Required cases (the registry may add more):
  - token_lifecycle: forged or random token; expired token (time moved past
    the 5 minute TTL by patching `django.utils.timezone.now`, never by
    sleeping); replay of a token after a successful execute; token presented
    with a different policy name, then the correct call (token must be burned).
  - parameter_binding: execute with a different param value than the preview;
    an extra undeclared param key; type confusion (bool for an integer param,
    `5.0` for an `integer_only` param, numeric string for a number param);
    out-of-bounds value.
  - row_set_fingerprint: a matching row inserted after preview; a previewed row
    deleted after preview; a fingerprint field mutated in place after preview
    (with `DB_PURGE_MCP_FINGERPRINT_FIELDS` configured).
  - allowlist_scope_escape: model removed from `DB_PURGE_MCP_ALLOWED_MODELS`
    between preview and execute; empty allowlist; a policy naming a model
    outside the allowlist (config must fail closed); tool calls carrying
    `model`, `filter`, or `select` arguments through the FastMCP client
    (schema must reject them).
  - row_cap: preview whose count exceeds the effective cap (refused, no token
    issued); policy `max_rows` lower than `DB_PURGE_MCP_MAX_ROWS` is honored;
    `DB_PURGE_MCP_MAX_ROWS = 0` fails closed; aggregate row cap bypass across
    multiple preview and execute cycles (the roadmap's suspected gap, declared
    `expected="finding"`).
  - protocol_injection: payloads from the static corpus sent through an
    in-memory `fastmcp.Client` as policy names and as param values; CR/LF in a
    policy name must not forge a second call-log line; an oversized string
    argument; a Unicode homoglyph of a real policy name.
- `tests/adversarial/payloads.json`: the static injection payload corpus
  (prompt injection text, SQL fragments, path traversal, CR/LF, NUL, homoglyphs,
  very long strings). Static data only. No live agent loop.
- `tests/adversarial/attacks.py`: one function per registry case id, mapped by
  id. Each function runs the attack against the real tool functions (or the
  in-memory FastMCP client for protocol cases) and returns an observed outcome
  (`blocked` or `not_blocked`) and a short evidence string. Evidence must be
  deterministic: no tokens, timestamps, durations, or primary keys.
  Harness-only policies and allowlists are applied with `override_settings`,
  using the existing toy models in `tests/models.py`. The token store
  (`_TOKENS`) is cleared before each case.
- `tests/adversarial/attacks_test.py`: the pytest entry point. Every test
  carries the `adversarial` marker. It is parametrized over the registry, uses
  pytest-django's database fixture, and asserts the observed outcome matches
  the case's `expected`:
  - `expected="blocked"` but not blocked: the test fails. The fix is a registry
    change to `expected="finding"` with severity and proposed fix, made by a
    human, never a change under `dbpurge/`.
  - `expected="finding"` but now blocked: the test fails (the registry is
    stale).
  - Every registry id has an attack function, and every attack function has a
    registry id. A test asserts both.
- `tests/adversarial/conftest.py`: adds a `--adversarial-json PATH` option that
  writes one JSON record per case, sorted by id, with keys `id`, `category`,
  `title`, `expected`, `observed`, `passed`, `evidence`, `severity`,
  `proposed_fix`. No timestamps or durations, so two runs produce identical
  bytes.
- `tests/adversarial/report.py`: `python -m tests.adversarial.report
  RESULTS_JSON` rewrites the generated region of `evals/README.md` between
  `<!-- adversarial-results:start -->` and `<!-- adversarial-results:end -->`.
  The region holds a results table (ID, Category, Attack, Expected, Observed,
  Result) and a findings table (ID, Severity, Finding, Proposed fix) built from
  cases with `observed="not_blocked"`. `--check` exits non-zero without writing
  if the file would change.
- `evals/README.md`: a hand-written threat model (assets, actors including a
  prompt-injected agent and a malicious MCP client, trust boundaries, what is
  out of scope, how each of the six categories maps to a threat), how to run
  the suite, how findings are recorded, and the generated region.
- `pyproject.toml`: a `[tool.pytest.ini_options]` table registering the
  `adversarial` marker and setting `DJANGO_SETTINGS_MODULE = "tests.settings"`.
- `.github/workflows/ci.yml`: a new `adversarial` job (Python 3.12, SQLite)
  that installs `".[mcp]"` plus exact-pinned `pytest` and `pytest-django`,
  runs the suite with `--junitxml` and `--adversarial-json`, runs
  `report --check`, and uploads both result files with
  `actions/upload-artifact@v4` even when an earlier step fails.
- `README.md`: a short subsection under Development describing the
  adversarial suite and linking to `evals/README.md`.

### Out

- Any change under `dbpurge/`. A case the server fails to block is recorded as
  a finding with severity and proposed fix. It is never patched in this task.
- A live agent loop or any LLM call. That would be a separate opt-in script and
  is not built here.
- Changes to `runtests.py`, the existing `tests/test_*.py` modules, or other CI
  jobs.
- `docs/roadmap.md`.
- PostgreSQL runs of the adversarial suite.

## Files touched

- `tests/adversarial/registry.py` (new, first implementation commit, alone)
- `tests/adversarial/__init__.py` (new, empty)
- `tests/adversarial/payloads.json` (new)
- `tests/adversarial/attacks.py` (new)
- `tests/adversarial/attacks_test.py` (new)
- `tests/adversarial/conftest.py` (new)
- `tests/adversarial/report.py` (new)
- `evals/README.md` (new)
- `pyproject.toml`
- `.github/workflows/ci.yml`
- `README.md`

No file under `tests/adversarial/` may match the Django runner's `test*.py`
discovery pattern, and none of `tests/test_*.py` may import pytest, so
`python runtests.py` keeps working in CI matrix entries that do not install
pytest.

## Acceptance criteria

1. `git log --reverse --format=%H origin/master..HEAD` lists the spec commit
   first; `git show --stat --format= <second commit>` lists only
   `tests/adversarial/registry.py`.
2. This prints six categories, each with a count of at least 3, and no
   duplicate ids:
   `python -c "import collections, tests.adversarial.registry as r; ids=[c.id for c in r.CASES]; assert len(ids)==len(set(ids)); print(collections.Counter(c.category for c in r.CASES))"`
3. Every case with `expected="finding"` has a severity in
   `{low, medium, high, critical}` and a non-empty `proposed_fix`; every
   `blocked` case has neither. The `row_cap` category contains the aggregate
   bypass case.
4. `pytest -m adversarial tests/adversarial --junitxml=junit.xml
   --adversarial-json=results.json` exits 0 and writes both files. The JSON has
   exactly one record per registry case.
5. Running the command in 4 twice and comparing the JSON files with `cmp`
   reports no difference.
6. `python -m tests.adversarial.report results.json --check` exits 0 on the
   committed `evals/README.md`.
7. `evals/README.md` contains a `## Threat model` heading, the two
   `adversarial-results` markers, and a results table with one row per case.
8. `git diff --stat origin/master...HEAD -- dbpurge/` prints nothing.
9. `find tests/adversarial -name 'test*.py'` prints nothing, and
   `grep -l pytest tests/test_*.py` prints nothing.
10. `grep -rnE 'time\.sleep|import (socket|requests|httpx)|anthropic|openai'
    tests/adversarial` prints nothing (hermetic, no LLM calls).
11. `.github/workflows/ci.yml` has an `adversarial` job running the commands
    in 4 and 6 and uploading `junit.xml` and `results.json` with
    `if: always()`. `pytest` and `pytest-django` are pinned with `==`.
12. `grep -nP '\x{2014}' evals/README.md README.md tests/adversarial/*` prints
    nothing (no em dashes).
13. `python runtests.py`, `ruff check .`, `ruff format --check .`, and `mypy`
    all pass.

## Tests to add

- The parametrized adversarial cases in `tests/adversarial/attacks_test.py`,
  one per registry case.
- A test that the registry ids and the attack function ids are the same set.
- A test that the registry satisfies criteria 2 and 3 (categories, minimum
  counts, finding fields).
- A test that `report.py` renders a fixed small JSON input to the expected
  markdown and that `--check` detects a stale region.

These are pytest tests with the pytest-django `db` fixture rather than Django
`TestCase` classes, because the roadmap decides the suite runs under its own
pytest marker and parametrization over the registry needs pytest. The existing
Django `TestCase` suite is unchanged.

## Docs to update

- `evals/README.md` (new): threat model, how to run, findings policy, generated
  results.
- `README.md`: Development subsection pointing to the adversarial suite.
- No em dashes in any new docs, docstrings, or comments.

## Roadmap item addressed

adversarial-harness (docs/roadmap.md, Sequenced queue). Verify marker:
`evals/README.md` on master contains a threat model and a results table.
