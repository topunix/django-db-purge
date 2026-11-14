# readme-reorg

## Goal

Reorganize README.md so a first-time reader meets the project in this order:
what it is, what state it is in, what it offers, how to install it, how to use
the MCP server, how to use the scheduled command, where its limits are, and how
to contribute. Documentation only. No behavior changes.

## Scope

In:

- Reorder README.md into the section order: title and badges, intro, status
  note, Features, entry point table, Requirements, Installation, MCP server,
  Scheduled purging (cron), Known limits, Development, Contributing, License.
- Move the existing Tools, Safety handshake, Settings, Running the server,
  Claude Desktop JSON snippet and its venv note, Inspecting the server, and
  Running the tests sections word for word. Running the tests becomes part of
  an H2 "Development" section.
- Add new text: badges, intro, status note, Features, entry point table,
  Requirements, Installation, Try it, Example session, Claude Code host
  configuration, the cron section and its settings table, Known limits,
  Contributing, License.
- Capture the example session from a real run of the server's tools through an
  in-process `fastmcp.Client` against the demo database. Reformat JSON for
  width only.
- Make every link to DESIGN.md an absolute GitHub URL on master, so it
  resolves on PyPI.

Out:

- Any change under dbpurge/ or tests/.
- docs/roadmap.md.
- The capture script and demo.sqlite3, which are not committed.
- Version bump, migration note, and PyPI publish. Those stay in release-2-0.
- Pushing, opening a pull request.

## Files touched

- README.md
- docs/tasks/readme-reorg.md (this file)

## Acceptance criteria

- Line 1 of README.md is `# django-db-purge`. No stray `---` lines. No heading
  ends in a colon.
- Development, Contributing, and License are H2 sections, not under MCP server.
- No setting, tool, or handshake statement is lost or changed in meaning.
- Every command in "Try it" runs from a bare checkout.
- Every Known limits claim is checked against DESIGN.md and the code.
- The example session JSON comes from a real run, with values unaltered.
- No em dashes anywhere. No person is named.
- `git diff --stat master` lists README.md and docs/tasks/readme-reorg.md only.
- `python runtests.py` passes.
- `python -m build` then `twine check dist/*` passes.

## Tests to add

None. This is a documentation change, and the test suite must still pass
unchanged.

## Docs to update

- README.md, as described above.

## Roadmap item addressed

None in full. This carves the README reorganization out of release-2-0, which
keeps the version bump, the migration note, and the publish.
