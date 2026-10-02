"""Rule table: stable rule ids, their outcome, and the detail keys their reason shows.

Every reason string is produced by render_reason() from this table, never by an LLM
(hard rule 5). Format:

    Rule R1 | cap=80000 | total=92000 | committed=0 | DENY

Money values are raw integer minor units (HKD cents), exactly as the gate compared them.
make_decision() refuses details that do not match the rule's keys, so a template can never
silently drift from the code that fills it.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.contracts import Decision


@dataclass(frozen=True)
class Rule:
    name: str
    outcome: str            # allow | deny | escalate
    keys: tuple[str, ...]   # detail keys, in the order the reason shows them


RULES: dict[str, Rule] = {
    # --- the nine policy rules ---
    "R1": Rule("budget_cap", "deny", ("cap", "total", "committed")),
    "R2": Rule("platform_allow_list", "deny", ("platform",)),
    "R3": Rule("category_allow_list", "deny", ("category",)),
    "R4": Rule("freelancer_eligibility", "deny", ("freelancer", "check", "value", "required")),
    "R5": Rule("milestone_only", "deny", ("milestones", "milestone_sum", "price")),
    "R6": Rule("no_offplatform_payment", "deny", ("channel",)),
    "R7": Rule("expiry", "deny", ("now", "issued_at", "expires_at")),
    "R8": Rule("revocation", "deny", ("policy_version", "revoked_at", "latest_version")),
    "R9": Rule("velocity", "deny", ("orders", "window_minutes", "max_orders")),
    # --- time-of-check vs time-of-use ---
    "R10": Rule("price_changed", "deny", ("approved_price", "current_price")),
    # --- supporting checks ---
    "R11": Rule("hire_limit", "deny", ("hires", "max_hires")),
    "R12": Rule("auto_approve_limit", "escalate", ("total", "limit", "escalation_expires_at")),
    "R13": Rule("quote_unverified", "escalate", ("freelancer", "price", "found", "escalation_expires_at")),
    "R14": Rule("proposal_invalid", "deny", ("problem",)),
    "R15": Rule("currency_fx", "deny", ("problem",)),
    "R16": Rule("message_limits", "deny", ("freelancer", "problem")),
    "R17": Rule("mandate_signature", "deny", ("policy_id",)),
    "R0": Rule("input_invalid", "deny", ("error",)),
    # --- allows ---
    "OK_HIRE": Rule("hire_within_policy", "allow", ("freelancer", "total", "committed", "cap")),
    "OK_MESSAGE": Rule("message_within_policy", "allow", ("freelancer", "sent", "max_messages")),
    "OK_RELEASE": Rule("release_within_policy", "allow", ("proposal", "total", "committed", "cap")),
    "OK_MANDATE": Rule("mandate_signature_valid", "allow", ("policy_id",)),
}

Detail = str | int | bool | None


def make_decision(rule_id: str, **details: Detail) -> Decision:
    """Build a Decision. Raises if the details don't match the rule's keys exactly."""
    rule = RULES[rule_id]
    if set(details) != set(rule.keys):
        raise ValueError(f"{rule_id} needs details {rule.keys}, got {tuple(details)}")
    return Decision(outcome=rule.outcome, rule_id=rule_id, details={k: details[k] for k in rule.keys})


def render_reason(decision: Decision) -> str:
    rule = RULES[decision.rule_id]
    parts = [f"{k}={decision.details.get(k)}" for k in rule.keys]
    return " | ".join([f"Rule {decision.rule_id}", *parts, decision.outcome.upper()])
