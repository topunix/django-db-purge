# lint-config

## Goal
Add ruff and mypy configuration, bring the existing code into compliance without
changing behavior, and enforce both on every pull request.

## Scope

In:
- `pyproject.toml` with tool configuration only.
- Formatting, lint fixes, and type fixes to existing code.
- A `lint` job in `.github/workflows/ci.yml`.

Out:
- `django-stubs` and the mypy Django plugin.
- Type checking `tests/`.
- Any change to `publish.yml`. Lint is not part of the release gate.
- Pre-commit hooks.
- Any behavior change. This branch is formatting, typing, and exception chaining only.

## Configuration
- `pyproject.toml` holds `[tool.ruff]`, `[tool.ruff.lint]`, and `[tool.mypy]` only. It
  has no `[build-system]` or `[project]` table. Packaging stays in `setup.py`.
- Ruff: `line-length = 88`, `target-version = "py310"`, rule sets
  `E, F, W, I, B, UP, SIM`.
- Ruff formatter is enforced with `ruff format --check`.
- Mypy: `strict = true`, `python_version = "3.10"`, checks `dbpurge` only.
- Mypy runs with the `[mcp]` extra installed so fastmcp imports resolve.
- Django has no type information without stubs. Allow its untyped imports with a
  scoped override for `django.*`, not a global `ignore_missing_imports`.

## Rules for suppressions
- No blanket ignores. Every per-file ignore, `# noqa`, and `# type: ignore` names the
  specific code and carries a one-line reason.
- List every suppression in the pull request body.

## Rules for fixes
- No logic changes. The existing test suite must pass with no test edits other than
  formatting and import order.
- `raise` inside `except` gains `from exc` or `from None`, chosen per case. Say which
  and why for each.
- If satisfying mypy requires restructuring the optional fastmcp import fallback
  (`_MissingMCP` and the names assigned beside it), show that diff on its own before
  committing it. That code decides what happens when the extra is missing.

## CI
- New job `lint` in `ci.yml`, Python 3.12, installs the package with `[mcp]`, then
  runs `ruff check .`, `ruff format --check .`, and `mypy`.
- Ruff and mypy are pinned to exact versions in the workflow so a new release cannot
  break CI on an unrelated branch.

## Accepted limitation
Without `django-stubs`, Django model, queryset, and settings types are `Any`, so
strict mode does not check them. Strict still covers this package's own signatures and
the token and policy logic.

## Commit plan
1. Add `pyproject.toml`.
2. Formatter only: `ruff format .`. Nothing but mechanical reformatting.
3. Ruff safe autofixes, then manual lint fixes.
4. Mypy fixes.
5. Add the `lint` job to `ci.yml`.
6. `CLAUDE.md`: add the three lint commands and the suppression rule.
7. Roadmap: delete `lint-config`, and add under "Unscheduled":
   "- **django-stubs**: add Django type stubs and the mypy plugin so strict mode
   covers model and queryset types."

## Files touched
- `pyproject.toml` (new)
- `.github/workflows/ci.yml`
- `dbpurge/**/*.py`, `tests/**/*.py`, `runtests.py`, `setup.py` (formatting and fixes)
- `CLAUDE.md`, `docs/roadmap.md`

## Acceptance criteria
1. `ruff check .` exits 0.
2. `ruff format --check .` exits 0.
3. `mypy` exits 0 with strict mode on `dbpurge`.
4. The full test suite passes with no assertion changed.
5. `publish.yml` is unchanged.
6. The wheel smoke test still passes with `pyproject.toml` present.
7. Every suppression names a code and a reason, and the pull request body lists them.
8. On the pull request, the `lint` job and all existing CI jobs pass.

## Tests to add
None. The `lint` job and the unchanged test suite are the checks.

## Docs to update
- `CLAUDE.md`: how to run ruff and mypy locally, and the suppression rule.

## Roadmap item addressed
`lint-config`. Delete it from `docs/roadmap.md` in this PR once the `lint` job runs
ruff and mypy.
