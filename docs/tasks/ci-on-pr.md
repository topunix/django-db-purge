# ci-on-pr

## Goal
Run the test suite and the wheel smoke test on every pull request and on every push to
`master`, so a broken branch is caught before merge instead of at release time.

## Scope

In:
- A new workflow, `.github/workflows/ci.yml`.

Out:
- Any change to `publish.yml`. It keeps its own test and smoke jobs as the release
  gate.
- Ruff, mypy, or any lint step (lint-config).
- Branch protection settings, README badges, coverage.

## Workflow requirements
- Triggers: `pull_request`, and `push` to `master`.
- `permissions: contents: read`. No secrets.
- A concurrency group per ref that cancels superseded runs.
- Job `test`: the same matrix and steps as the `test` job in `publish.yml`.
- Job `wheel-smoke-test`: the same steps as the job of that name in `publish.yml`.
- No publish or build-for-upload step.

## Accepted limitation
The test and smoke steps are duplicated between `ci.yml` and `publish.yml`. A reusable
workflow would remove the duplication but means editing the release path, which is out
of scope here.

## Files touched
- `.github/workflows/ci.yml` (new)
- `docs/roadmap.md`

## Acceptance criteria
1. `ci.yml` parses as valid YAML and declares both triggers.
2. The `test` matrix in `ci.yml` equals the `test` matrix in `publish.yml`.
3. The `wheel-smoke-test` steps in `ci.yml` match those in `publish.yml`.
4. `publish.yml` is unchanged.
5. On the pull request for this branch, every `ci.yml` job runs and passes.

## Tests to add
None. The workflow run on this branch's pull request is the test.

## Docs to update
None.

## Roadmap item addressed
`ci-on-pr`. Delete it from `docs/roadmap.md` in this PR.
