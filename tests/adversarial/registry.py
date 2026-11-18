"""
Declarative registry of attack cases against the MCP purge surface.

Plain data with stdlib imports only, so it can be reviewed and revised
without running anything. Each case names one attack and what the server
is expected to do about it. tests/adversarial/attacks.py holds the code
that mounts each attack, keyed by case id.

expected="blocked" means the server stops the attack. expected="finding"
means a gap is known or suspected: the case then carries a severity and a
proposed fix, and the suite asserts the gap is still open so a stale
entry is noticed. A case the server fails to block is recorded here as a
finding by a human. It is never patched away inside this harness.
"""

from dataclasses import dataclass
from typing import Literal

Category = Literal[
    "token_lifecycle",
    "parameter_binding",
    "row_set_fingerprint",
    "allowlist_scope_escape",
    "row_cap",
    "protocol_injection",
]
Expected = Literal["blocked", "finding"]
Severity = Literal["low", "medium", "high", "critical"]

CATEGORIES: tuple[str, ...] = (
    "token_lifecycle",
    "parameter_binding",
    "row_set_fingerprint",
    "allowlist_scope_escape",
    "row_cap",
    "protocol_injection",
)
SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")


@dataclass(frozen=True)
class Case:
    """One attack: what is tried, why, and what the server should do."""

    id: str
    category: Category
    title: str
    threat: str
    expected: Expected = "blocked"
    severity: Severity | None = None
    proposed_fix: str | None = None


CASES: tuple[Case, ...] = (
    # token_lifecycle
    Case(
        id="token-forged",
        category="token_lifecycle",
        title="Execute with a forged or random confirmation token",
        threat=(
            "Delete rows without ever previewing them by guessing or "
            "fabricating a token."
        ),
    ),
    Case(
        id="token-expired",
        category="token_lifecycle",
        title="Execute with a token older than the 5 minute TTL",
        threat=(
            "Reuse a stale preview, after the data has moved on, to delete "
            "rows nobody has looked at recently."
        ),
    ),
    Case(
        id="token-replay-after-execute",
        category="token_lifecycle",
        title="Replay a token after a successful execute",
        threat="Turn one approved preview into repeated deletions.",
    ),
    Case(
        id="token-wrong-policy-burned",
        category="token_lifecycle",
        title="Present a token with another policy name, then the correct call",
        threat=(
            "Probe whether a token survives a mismatched attempt so it can "
            "be retried with adjusted arguments."
        ),
    ),
    # parameter_binding
    Case(
        id="binding-different-value",
        category="parameter_binding",
        title="Execute with a different param value than the preview",
        threat=(
            "Preview a narrow window, then execute a wider one under the same token."
        ),
    ),
    Case(
        id="binding-extra-param",
        category="parameter_binding",
        title="Execute with an extra undeclared param key",
        threat="Smuggle an unvetted argument past the policy's declared params.",
    ),
    Case(
        id="binding-type-bool",
        category="parameter_binding",
        title="Supply a bool where an integer param is declared",
        threat="Exploit bool being an int subclass to sneak in 0 or 1.",
    ),
    Case(
        id="binding-type-float-for-integer",
        category="parameter_binding",
        title="Supply 5.0 for an integer_only number param",
        threat=(
            "Use a float that equals an integer to hash or compare "
            "differently from the previewed value."
        ),
    ),
    Case(
        id="binding-type-numeric-string",
        category="parameter_binding",
        title="Supply a numeric string for a number param",
        threat="Rely on implicit coercion of text to a number at the database.",
    ),
    Case(
        id="binding-out-of-bounds",
        category="parameter_binding",
        title="Supply values outside the declared min and max",
        threat="Widen a purge past the bounds the operator set for the agent.",
    ),
    # row_set_fingerprint
    Case(
        id="fingerprint-phantom-insert",
        category="row_set_fingerprint",
        title="A matching row is inserted after preview",
        threat=(
            "Get a row deleted that the reviewer never saw by creating it "
            "between preview and execute."
        ),
    ),
    Case(
        id="fingerprint-row-deleted",
        category="row_set_fingerprint",
        title="A previewed row is deleted after preview",
        threat=(
            "Let the executed set silently shrink so the approval no longer "
            "describes what happened."
        ),
    ),
    Case(
        id="fingerprint-in-place-mutation",
        category="row_set_fingerprint",
        title="A fingerprinted column is mutated in place after preview",
        threat=(
            "Change what a previewed row contains, keeping its primary key, "
            "so the purge hits different data than was approved."
        ),
    ),
    # allowlist_scope_escape
    Case(
        id="allowlist-model-removed-after-preview",
        category="allowlist_scope_escape",
        title="Model leaves the allowlist between preview and execute",
        threat="Execute against a model the operator has since withdrawn.",
    ),
    Case(
        id="allowlist-empty",
        category="allowlist_scope_escape",
        title="Empty allowlist with a policy configured",
        threat="Purge anything at all when the operator allowed nothing.",
    ),
    Case(
        id="allowlist-policy-model-outside",
        category="allowlist_scope_escape",
        title="A policy names a model outside the allowlist",
        threat=(
            "Reach a model the operator never allowed through a mistaken or "
            "hostile policy entry, which must fail closed."
        ),
    ),
    Case(
        id="scope-model-argument",
        category="allowlist_scope_escape",
        title="Tool call carries a model argument",
        threat="Name the model to purge instead of selecting a policy.",
    ),
    Case(
        id="scope-filter-argument",
        category="allowlist_scope_escape",
        title="Tool call carries a filter argument",
        threat="Supply a custom row filter, such as an always-true one.",
    ),
    Case(
        id="scope-select-argument",
        category="allowlist_scope_escape",
        title="Tool call carries a select argument",
        threat="Override which rows the policy selects.",
    ),
    # row_cap
    Case(
        id="cap-preview-exceeds",
        category="row_cap",
        title="Preview whose count exceeds the effective cap",
        threat="Obtain a token for a purge larger than the cap allows.",
    ),
    Case(
        id="cap-policy-max-rows-lower",
        category="row_cap",
        title="Policy max_rows lower than the global cap is honored",
        threat="Delete up to the global cap when the policy set a tighter one.",
    ),
    Case(
        id="cap-global-zero-fails-closed",
        category="row_cap",
        title="DB_PURGE_MCP_MAX_ROWS set to 0",
        threat="Read a zero cap as unlimited and delete without a bound.",
    ),
    Case(
        id="cap-aggregate-bypass",
        category="row_cap",
        title="Delete far more than the cap across many preview and execute cycles",
        threat=(
            "Stay under the per-call cap on every call while the total "
            "deleted is unbounded."
        ),
        expected="finding",
        severity="medium",
        proposed_fix=(
            "Add a rolling-window budget, for example "
            "DB_PURGE_MCP_WINDOW_MAX_ROWS over DB_PURGE_MCP_WINDOW_SECONDS, "
            "tracked per policy at execute and failing loudly when the "
            "running total would exceed it."
        ),
    ),
    # protocol_injection
    Case(
        id="injection-policy-name-payloads",
        category="protocol_injection",
        title="Static corpus payloads sent as policy names",
        threat=(
            "Use prompt injection, SQL, path traversal or control characters "
            "in the policy name to reach something other than a policy."
        ),
    ),
    Case(
        id="injection-param-value-payloads",
        category="protocol_injection",
        title="Static corpus payloads sent as param values",
        threat="Inject SQL or control text through a param value.",
    ),
    Case(
        id="injection-crlf-log-forgery",
        category="protocol_injection",
        title="CR and LF in a policy name forge a second call-log line",
        threat="Plant a fake log record to hide or fabricate a destructive call.",
    ),
    Case(
        id="injection-oversized-string",
        category="protocol_injection",
        title="Oversized string argument",
        threat="Exhaust memory, logs or the database with a huge argument.",
        expected="finding",
        severity="low",
        proposed_fix=(
            "Bound agent-supplied text before it reaches the call log: "
            "truncate the repr of policy_name and params in _log_call to a "
            "few hundred characters, and cap policy_name and "
            "confirmation_token length in the tool signatures."
        ),
    ),
    Case(
        id="injection-homoglyph-policy-name",
        category="protocol_injection",
        title="Unicode homoglyph of a real policy name",
        threat="Pass a lookalike name that a human or filter would read as real.",
    ),
)
