# Design decisions

This document records the architectural decisions behind the MCP server
in `dbpurge/management/commands/purge_mcp_server.py`, in the order they
were made. Each entry states the problem, the option chosen, the options
rejected and why, and any limitation accepted along with the choice.

The through line: an AI agent calls these tools, and an agent can be
wrong, confused, or manipulated by whatever it read upstream. The design
assumes the caller is not trustworthy and that a human approves the one
destructive step. Everything below follows from that.

---

## ADR 1: The preview, token, execute workflow

*Amended by ADR 4: the tools now take a policy name, and the preview sample is the primary key plus each param's value, keyed by param name, not the time column.*

### Problem

`execute_purge` deletes rows. The host LLM decides when to call it and
with what arguments. A single tool call that both chooses a row set and
destroys it gives a human no point at which to look at what is about to
happen and say no. Prompt injection in a document the agent read, a
misread retention policy, or a hallucinated column name all lead
straight to data loss with no intervening checkpoint.

### Chosen

Split the operation into two tools, with a mandatory handshake between
them. `preview_purge` validates the policy, runs the retention filter,
and returns the matching row count plus a small sample (primary key and
the time column only, never full row contents), along with a
`confirmation_token`. `execute_purge` refuses to do anything without a
token from a prior matching preview. There is no code path from the
tool surface to a `DELETE` that does not pass through a preview first.

Deletion is therefore always preceded by a tool result the human can
read in their MCP client before approving the second call. The preview
output is the artifact the human actually reviews, so it deliberately
carries counts and samples rather than a bare "N rows will be deleted".

### Rejected

**A single `purge` tool with a `dry_run: bool` parameter.** The agent
controls the parameter, so the safety property reduces to "the agent
chose to be careful". An agent that flips `dry_run` to `False` on its
first call has done nothing the schema forbids. Safety must not be a
parameter the untrusted caller sets.

**A confirmation prompt inside the tool.** MCP tools return values,
they do not conduct interactive dialogue with the human, and anything
the tool "asks" is answered by the model, not the person. This just
relocates the same problem.

**Relying on the MCP client's own tool-approval UI.** Good clients do
prompt before a tool call, but that is a property of the client, not of
this server. A server whose safety depends on which host happens to
call it is not safe. The handshake holds regardless of host.

### Accepted limitations

The human has to actually read the preview. Nothing here forces
comprehension, and an operator who approves every tool call by reflex
gets no protection from the handshake. The design buys a checkpoint,
not attention.

---

## ADR 2: What the token binds, and how it is stored

*Amended by ADR 4: the token binds a SHA-256 of canonical JSON of the policy name and the validated params, not the `(app_name, model_name, time_column, retention_seconds)` tuple.*

### Problem

Once tokens exist, a token is a capability: whoever holds one can
delete. The question is what a token authorizes. A token that means
"some purge was previewed" is nearly worthless, since an agent could
preview a harmless one-row purge and then use that token to execute a
million-row one.

### Chosen

The token binds the exact parameter tuple that produced it. The record
stores a SHA-256 hash of `(app_name, model_name, time_column,
retention_seconds)`, and `execute_purge` recomputes that hash from its
own arguments and requires an exact match. Changing any argument
invalidates the token, even an unexpired one.

Tokens carry a 5 minute TTL, so an approval does not stay live
indefinitely, and expired records are evicted whenever a new token is
issued so the store cannot grow without bound.

Validation is an atomic pop: `_pop_valid_token` removes the token from
the store *before* checking expiry and parameters, so two racing
`execute_purge` calls cannot both see it as valid. A caller that then
fails for a reason that is not its own fault puts the record back via
`_reinstate_token`. The rule is that a token is consumed only by a
delete that actually succeeded.

Failures are deliberately indistinguishable. An unknown token, an
expired token, and a parameter mismatch all raise the identical
`"invalid or expired confirmation token"` message, so error content
cannot be used to probe TTLs or hunt for near-miss parameter matches.

Storage is a module-level dict in the server process.

### Rejected

**A signed, stateless token (HMAC or JWT style) carrying its own
parameters.** Attractive because it needs no server state, but a
stateless token cannot be revoked or single-used without a server-side
seen-list, which puts the state right back. Single use is the property
worth more here than statelessness.

**Binding the token to the model only, not the full parameter tuple.**
Retention seconds is exactly the argument whose drift is most
dangerous: previewing a 90 day retention and executing a 1 day one
deletes vastly more than what was approved.

**Checking validity and then deleting the token afterwards
(check-then-pop).** That is the classic time-of-check to time-of-use
window: two concurrent executes both pass the check before either
consumes. Popping first closes it.

**Distinct error messages per failure cause.** Friendlier for a
legitimate caller, but it hands an attacker an oracle. The debugging
loss is real and accepted.

**Redis or a database table for the token store.** Correct for a
multi-process deployment, and unnecessary complexity for a stdio server
where one client owns one process for the whole session.

### Accepted limitations

The in-process dict is the significant one, and it is called out in a
comment at the definition of `_TOKENS`. If this server ever grows a
streamable HTTP transport with multiple workers, or restarts between a
preview and an execute, tokens issued by one process are invisible to
the others and every execute fails. That migration to shared storage is
a known prerequisite for the HTTP phase, not an oversight.

Token TTL is a fixed constant rather than a setting. Nobody has needed
otherwise yet.

---

## ADR 3: Row-set fingerprinting

*Amended by ADR 4: rows are now taken in primary key order, not `(time_column, pk)` order, and the filter is the policy's conditions rather than a time column and cutoff.*

### Problem

Binding the parameters is not the same as binding the rows. The
parameters describe a filter, and a filter is evaluated against a
database that other processes are writing to. Between the preview a
human approved and the execute that acts on it, the set of rows
matching `time_column <= cutoff` can change:

- Rows written before preview age past the cutoff by the time execute
  runs, so they join the match set (a phantom).
- Rows in the previewed set are deleted or updated by another writer,
  so they leave it.
- Rows in the previewed set are mutated in place while still matching.

Worse, the cutoff itself is recomputed at execute time from
`timezone.now()`, so the filter is not even the same filter: it is a
sliding window that necessarily admits rows the preview never saw.
A human approved "these 40 rows"; the original implementation then
re-ran the filter and deleted whatever it matched, which could be a
different and larger set. The parameters matched perfectly the whole
time.

### Chosen

Two mechanisms, with distinct jobs.

**1. Delete by primary key list (the safety mechanism).** At preview,
capture the ordered primary keys of the matched rows and persist that
list server-side in the token record. At execute, delete with
`model.objects.filter(pk__in=record.pk_list).delete()`, never by
re-running the retention filter. This makes phantom inserts
structurally harmless rather than merely detected: a row that joins the
match set after preview is not in the list, so no code path can delete
it. The guarantee holds even if the drift check below were removed
entirely.

**2. Fingerprint comparison (the drift notification).** Hash the
ordered PK list with SHA-256 and bind the digest into the token record
next to the parameter hash. Inside the execute transaction, before
deleting, re-run the filter and recompute the fingerprint. On mismatch,
abort with `PurgeRowSetChangedError`, a distinct error type stating
that the row set changed since preview and naming both the previewed
and the current row count. This tells the operator the world moved
under their approval. It is not what makes the delete safe.

The ordering detail matters. Rows are selected in `(time_column, pk)`
order so that, when the match set exceeds the row cap, the slice taken
is deterministic and stable. They are then re-sorted by primary key
alone before the PK list and the hash are built, because
`time_column` values can change between preview and execute and would
otherwise reorder an unchanged set of rows, producing a false drift
rejection on a row set that is in fact identical.

Both counts reported in the mismatch error are true uncapped match
counts, taken before any truncation, on their respective sides. The cap
bounds what is stored and hashed, not what is reported, so the error
cannot claim two identical counts for a table whose real match set
exceeds the cap.

**Fingerprint scope: primary keys only by default.** An opt-in
per-model setting, `DB_PURGE_MCP_FINGERPRINT_FIELDS`, adds named column
values into the hash for callers who want in-place mutation detection.

**Locking is a nicety.** Where the backend supports it
(`connection.features.has_select_for_update`), the re-check runs under
`select_for_update()` so the candidate rows are held for the rest of
the transaction. Where it does not, notably SQLite, the call is simply
skipped. No error, no warning, no degraded mode, because delete-by-PK
already handles the case the lock would defend against.

**Drift reinstates the token.** A fingerprint mismatch takes the same
reinstate-on-failure path as a row-cap breach. Drift is not caller
error, the TTL already bounds how long a retry window stays open, and
the alternative would introduce a second token-consumption rule for no
safety gain. One rule stands: the token is consumed only by a
successful delete. Drift is always a rejection, never an automatic
retry or a silently reissued token; the caller must run `preview_purge`
again and get a fresh approval.

### Rejected

**Re-running the filter at execute and deleting its result, with the
fingerprint as the only guard.** This was the original behavior plus a
check. It makes correctness depend entirely on the check being right
and on the window between the check and the delete being empty. Neither
is true on a backend without row locking. Delete-by-PK is structural:
it does not depend on a comparison succeeding.

**Freezing the cutoff at preview time and reusing it at execute.**
Narrows the phantom window but does not close it, since a concurrent
insert can carry any timestamp it likes, backdated ones included. It
also silently changes what a retention policy means when a token is
executed near the end of its TTL. Delete-by-PK subsumes the benefit.

**`SELECT ... FOR UPDATE` as the primary mechanism.** Unavailable on
SQLite, which is what the test suite and plenty of small deployments
run. A safety property that exists only on some backends is not a
safety property, and building on it would have forced either a hard
failure on SQLite or a quietly weaker guarantee there. Making it
optional on top of a mechanism that already holds everywhere avoids
both.

**Hashing full row contents by default.** Catches every mutation, and
costs a full column read of every matched row on every preview and
every execute, on tables that may hold millions of rows. It also makes
drift rejections routine on any table with a `last_accessed` or
counter column that ordinary traffic touches, which trains operators
to re-preview reflexively until it passes. That is worse than not
checking.

**Requiring an `updated_at` or version column.** Not every model has
one. A mechanism that only works on well-behaved schemas would either
exclude models or force a migration on users who just want retention
cleanup.

**Auto-reissuing a token on drift, or retrying against the new row
set.** Both delete a row set no human ever saw. The entire point of the
handshake is that a person looked at a specific set of rows. If that
set changed, the approval no longer refers to anything, and the correct
answer is to go get a new one.

**Storing the PK list client-side, in the token string itself.** A
token for a 10,000 row purge would be enormous, and worse, it would put
the list under the caller's control. The agent could hand back a
modified list. Server-side storage keeps the approved set out of the
untrusted caller's reach.

### Accepted limitations

**Cascade targets are not fingerprinted.** This is the known gap, and
it is a real one. The fingerprint covers the primary keys of the
directly matched rows only. If a parent row is approved for deletion at
preview time and child rows referencing it accumulate before execute,
those new children are deleted along with the parent by
`ON DELETE CASCADE`, and no check anywhere notices. The row cap has the
same blind spot: it bounds matched rows, not cascade fan-out, so the
`deleted_count` returned can exceed the cap legitimately, since it is
`delete()`'s own total including cascades. Operators purging models
with cascading children should size their retention windows and caps
with that in mind. Fingerprinting the transitive cascade closure would
mean walking every related model on every preview, which is a
substantially larger and slower change than this one, and it is
deliberately not attempted here.

**The fingerprint re-check and the delete are still separate
statements.** On a backend without row locking, a writer can change the
match set in the gap between them. This costs less than it sounds:
because the delete targets a fixed PK list, the worst outcome is
deleting a row that was mutated in that window, never deleting a row
outside the approved set. On backends with `select_for_update`, the
gap is closed for the rows under lock.

**Bounded by the row cap.** The stored PK list is capped at
`DB_PURGE_MCP_MAX_ROWS`, deliberately, so the token store cannot be
made to hold an unbounded list. A purge whose match set exceeds the cap
fails on the cap check before the fingerprint is ever consulted, so the
truncation is not silently acted on.

**In-place mutation is invisible by default.** With PK-only
fingerprinting, a row whose contents change between preview and execute
but which still matches the filter is deleted without complaint. The
approved thing was "this row", and it is still that row. Callers who
disagree have `DB_PURGE_MCP_FINGERPRINT_FIELDS`.

---

## ADR 4: Operator-defined policies instead of agent-chosen filters

### Problem

The first interface took `app_name`, `model_name`, `time_column`, and
`retention_seconds` from the agent. That hands the untrusted caller the
choice of what to delete: any allowlisted model, any date column, any
retention. The allowlist was the only boundary, and the human reviewing
a preview saw a count and a few primary keys, which says little about
whether the purge being proposed is one the operator ever intended. The
interface could also express nothing but row age, so a purge by status
or by a numeric threshold had no place in it.

The threat is the same as in ADR 1: the caller can be wrong, confused,
or steered by whatever it read upstream. The less the request can say,
the less a bad request can do.

### Chosen

**The operator writes named policies in `DB_PURGE_MCP_POLICIES`, and the
agent selects one by name.** A policy fixes the model, the optional
`select` and `exclude` conditions, and up to two declared params with
bounds. The agent supplies only those params. It never names a model, a
column, or a filter, and no tool accepts `app_name`, `model_name`,
`time_column`, or `retention_seconds`. Row age becomes one kind of policy
(`age_seconds`) next to `number` and `choice` params.

**`select` and `exclude` are Django `Q` objects, written by the
operator.** They are code in the settings file, never text from a
request, and `list_policies` does not return them or the fields a param
filters on. `action` exists and defaults to `"delete"`, so a later verb
is additive, but nothing other than `"delete"` is accepted.

**Loading fails closed.** `load_policies()` validates the whole registry
and raises `ImproperlyConfigured` on the first violation, so a typo
disables every policy instead of dropping one quietly. It rejects
unknown keys, a model that is not in `DB_PURGE_MCP_ALLOWED_MODELS`, a
policy with neither `select` nor a param, an empty `Q`, a param on the
primary key, a relation, or a field name containing `__`, a param named
`pk`, a same-field pair that is not an opposite-direction pair, and a
`max_rows` above the global cap. It also compiles `select` and `exclude`
without running them, so a `Q` that names a missing field fails at load
and not in the middle of a preview. The management command loads once
before serving and turns a failure into a `CommandError`.

**Requests are validated on the values as received, before any query.**
The supplied keys must equal the declared names exactly, with no
defaults. Types are exact: a bool is never a number, NaN and infinity
are rejected, an integer column takes only ints, an age is an int, and a
choice must equal a declared entry by type and value. A same-field pair
must describe a non-empty window, and equal bounds are valid only when
both ends are inclusive. Error messages name only declared params and
never echo supplied text, and an unknown policy name is rejected without
being repeated or logged. FastMCP coerced a JSON `true` into the integer
1 for the declared `int | float | str` values, so the tool signatures use
pydantic strict types, which leaves the generated JSON schema unchanged
and lets the check see what the client sent.

**Policies are reloaded on every call.** There is no cache, so the
allowlist and the config are re-checked each time. If loading fails
during a call, the client gets one opaque error and the detail goes to
the server log.

**The token binds the policy name and the validated params.** The hash is
SHA-256 over canonical JSON (`sort_keys`, compact separators) of the
policy name and the validated params. That replaces the `|` join in ADR 2,
whose delimiter was ambiguous in principle, since a value containing it could collide with a different tuple. Values are
hashed as received, so `5` and `5.0` give different tokens. That can only
make a token fail to match, never match wrongly. A token presented with a
different policy or different params is popped and not reinstated, so the
attempt burns it. That is deliberate: a mismatch is a caller error, and
reinstating the token would turn it into an oracle for near-miss params.
Only failures after the token matches (row cap, drift, database errors)
reinstate it.

**The matched row set is one row per record.** `build_queryset` runs the
conditions in a subquery and selects the model by primary key, so a
`select` or `exclude` that joins a to-many relation cannot duplicate a
row, and no `DISTINCT` is needed. Some backends refuse `FOR UPDATE`
together with `DISTINCT`, so this keeps the optional lock from ADR 3
usable. A `DateField` param compares against the cutoff's calendar date,
not an aware datetime.

**Row cap per policy.** The effective cap is the policy's `max_rows` if
set, else `DB_PURGE_MCP_MAX_ROWS`, and both must be positive integers. A
preview whose count exceeds it is refused and no token is issued, and
execute re-checks it as before.

**Previews reveal values, not columns.** A sample row is the primary key
plus the value of each param, keyed by param name. `list_policies` shows
each param's name, type, op, and bounds or choices, and whether a number
param is integer-only, but no field names.

**The preview, single-use token, 5 minute TTL, atomic pop, delete by
stored primary key list, fingerprint, and drift rejection from ADR 1 to
3 are unchanged.** The snapshot now takes its slice in primary key order.

### Rejected

**Agent-supplied filters** (a JSON predicate, a Q-like DSL, or a SQL
fragment). The agent would then write the delete predicate, so a prompt
injection becomes an arbitrary predicate. Validating a filter language
safely is a large surface of its own, and every field and operator it
allowed would be one more way to reach rows the operator never meant to
expose. Declared, bounded params give the agent a few numbers to choose
and nothing to author.

**A `days` shim**, keeping the old signature as a deprecated alias for a
built-in age policy. It would keep the interface that lets the agent name
a model and a column alive as a second path to the same delete, so the
guarantee "no tool accepts a model or a column" would not hold. The old
call is a breaking change and is expressed as a policy instead.

**Dropping the allowlist, since policies already name their models.** A
policy is a sharper tool than a model name, and one that points at the
wrong model, through a typo or a copy and paste, would be live the moment
it loaded. The allowlist is a separate, short list an operator can read
at a glance, empty means nothing is purgeable, and requiring a model to
appear in both places means one slip cannot expose it.

**Caching the loaded policies at startup.** It would save a little work
per call, but the allowlist and config would then be checked once and
trusted afterwards. Loading per call keeps every call on the same
checked path.

**Reinstating a token after a mismatched execute.** It would be kinder to
a confused caller and would let a caller probe how close its params are
to the previewed ones.

**Defaults for params.** A missing param is an error, so the token and the
preview always bind the same, fully stated request.

**`DISTINCT` to deduplicate joins.** Some backends refuse `FOR UPDATE`
with it, which would have forced a weaker guarantee there.

### Accepted limitations

**The row cap does not bound an aggregate.** It is per call. An agent can
slice a purge across params, for example many narrow age windows, and
delete far more than `max_rows` over many previews and executes, each one
under the cap. Each call is still previewed, but nothing counts the total.
The adversarial harness item tracks this gap.

**A preview is a read oracle on the param fields.** An agent can choose
params and read back counts and sample values, so repeated previews can
map the distribution of those columns, within the bounds the operator set.
Params are the only fields exposed, and a policy that should not reveal a
column should not declare a param on it.

**Tokens are not bound to the policy definition.** The token binds the
policy name and the params, not the `select`, `exclude`, or `max_rows` in
force at preview. If the operator edits a policy between preview and
execute and restarts the server, the old token still matches by name. The
fingerprint re-check then runs against the new definition, so a changed
filter will usually surface as drift, but it is not guaranteed.

**Settings are frozen at startup.** Django reads settings once, so a
change to a policy or to the allowlist needs a restart. Per-call loading
re-validates what the process already holds and does not re-read the file,
so revocation is not immediate.

**The token store can grow.** Tokens live in process memory and are
evicted only when a later preview issues one, so many previews within the
5 minute TTL each hold up to `max_rows` primary keys until they expire.
That is bounded by the cap and the TTL but not by a count of previews. It
is part of the same in-process store described in ADR 2.

**Equal numbers of different type are different tokens.** `5` and `5.0`
bind differently, so a client that previews with one and executes with the
other is rejected and burned. That fails safe.

**The cap slice only affects the stored fingerprint.** Because an
over-cap execute is refused before the fingerprint is consulted, taking
the slice in primary key order, and not oldest first as in ADR 3, changes
only what a stored fingerprint covers.
