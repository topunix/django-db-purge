---
description: Implement roadmap items unattended, one PR per item
argument-hint: <item slugs or queue numbers, comma separated>
---
Implement these roadmap items from docs/roadmap.md, in the order given:
$ARGUMENTS

This run is unattended. Never wait for the user. Never merge. Never push to
master. Process every item even if an earlier one fails.

Resolve queue numbers to slugs against the current file and use slugs from
then on.

Before the first item, run git fetch origin master. Throughout this
command, "master" means origin/master as just fetched, never the local
master ref, which can be stale.

For each item:

1. Gate. Skip the item and record why if any of these hold:
   - Its Verify marker is already true on origin/master (roadmap is stale).
   - It has a "Resolve before starting" line, or any wording that needs a
     maintainer decision or a chat revision before implementation.
   - It depends on an item that is not yet on origin/master.

2. Branch. Run git fetch origin master, then create the branch named
   after the slug from origin/master.

3. Spec. Write docs/tasks/<slug>.md from the roadmap entry and the actual
   code, using the section layout of existing specs: Goal, Scope (In, Out),
   Files touched, Acceptance criteria, Tests to add, Docs to update, Roadmap
   item addressed. Acceptance criteria must be checkable by command or by
   reading a file. Honor every Decided and Rejected line in the roadmap
   entry. Commit the spec alone.

4. Implement. Delegate to the implementer agent with the spec path.

5. Review. Delegate to the reviewer agent with the spec path. On
   VERDICT: CHANGES, send blockers and majors back to the implementer, then
   review again. At most 3 rounds.

6. Roadmap. Do not edit docs/roadmap.md. The maintainer removes shipped
   entries after merging.

7. PR. Push the branch and open a PR against master. The body has: summary,
   link to the spec, acceptance criteria with results, Deviations, and
   unresolved reviewer findings. If review did not reach APPROVE in 3
   rounds, open it as a draft and say so in the title.

8. CI. Wait for the PR checks. On failure, have the implementer fix it and
   push, at most 3 attempts. An item is green only when checks pass and the
   reviewer approved.

Keep your own context small: hold per-item summaries, not file contents.

Finish with one report: a table of item, status (green, draft, skipped),
PR link, and reason, followed by the list of roadmap entries to delete once
merged.
