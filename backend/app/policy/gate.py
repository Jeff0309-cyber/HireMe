"""The deterministic policy gate. Every function here is PURE: no I/O, no clock, no LLM,
no randomness. Same inputs -> same Decision. Any exception becomes a deny (rule R0).

Entry points
  evaluate(policy, state, proposal, now)     may the agent hire? (proposal is a HireRequest)
  evaluate_message(policy, state, msg, now)  may the agent send this message?
  recheck_before_release(policy, state, now) is the approved hire still OK right before money
                                             moves? (time-of-check vs time-of-use)
  verify_mandate(signed, trusted_pubkey)     is the policy really signed by the user?

Hire rules, in the order they are checked (first failure decides):
  R0  input invalid          R7  expiry               R8  revocation
  R14 proposal invalid       R2  platform allow-list  R3  category allow-list
  R4  freelancer eligibility R6  no off-platform pay  R15 currency / FX
  R5  milestone-only         R13 quote unverified     R11 hire limit
  R9  velocity               R1  budget cap           R12 auto-approve limit -> OK_HIRE

This package must never import app.llm, app.agent or app.marketplace (a test enforces it).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.contracts import Decision, HireProposal, Message, Policy, Profile, SignedMandate
from app.money import convert_micros
from app.policy.actions import HireRequest
from app.policy.mandate import mandate_signature_ok
from app.policy.quotes import parse_quote
from app.policy.rules import make_decision


# --- state -----------------------------------------------------------------------------

class PendingRelease(BaseModel):
    """A hire the gate allowed but whose money has not moved yet."""
    model_config = ConfigDict(extra="forbid")
    proposal: HireProposal
    approved_total_minor: int                 # what evaluate() approved, fees and FX included
    current_price_minor: Optional[int]        # latest price the freelancer stated; None = unclear
    approved_at: datetime


class PolicyState(BaseModel):
    """Facts the gate needs about the world. Only the executor changes it."""
    model_config = ConfigDict(extra="forbid")

    committed_minor: int = 0                                     # spent on orders, budget currency
    order_times: list[datetime] = Field(default_factory=list)    # one per committed order
    messages_sent: dict[str, int] = Field(default_factory=dict)  # freelancer_id -> count
    revoked_at: Optional[datetime] = None                        # user revoked the policy from then on
    latest_policy_version: Optional[int] = None                  # a newer version supersedes this one
    pending: Optional[PendingRelease] = None


# --- shared helpers (also used by the agent, so they can never disagree) ----------------

@dataclass(frozen=True)
class EligibilityFailure:
    check: str
    value: str | int | bool
    required: str | int | bool

    @property
    def code(self) -> str:
        return f"R4.{self.check}"


def eligibility_failures(profile: Profile, policy: Policy) -> list[EligibilityFailure]:
    e = policy.eligibility
    out = []
    if profile.freelancer_id in policy.blocked_freelancers:
        out.append(EligibilityFailure("blocked", True, False))
    if profile.rating_x100 < e.min_rating_x100:
        out.append(EligibilityFailure("min_rating", profile.rating_x100, e.min_rating_x100))
    if profile.completed_orders < e.min_completed_orders:
        out.append(EligibilityFailure("min_completed_orders", profile.completed_orders, e.min_completed_orders))
    if e.verified_required and not profile.verified:
        out.append(EligibilityFailure("verified", False, True))
    return out


def escalation_expires_at(decided_at: datetime, policy: Policy) -> datetime:
    return decided_at + timedelta(seconds=policy.escalation_ttl_seconds)


def escalation_expired(decided_at: datetime, now: datetime, policy: Policy) -> bool:
    """An escalation nobody answered in time counts as a deny (fail closed)."""
    return now >= escalation_expires_at(decided_at, policy)


def proposal_total_minor(p: HireProposal) -> int:
    """Price plus fees, converted into the budget currency, each FX conversion rounded up."""
    def conv(amount: int) -> int:
        return amount if p.fx is None else convert_micros(amount, p.fx.rate_micros)
    return conv(p.price.amount_minor) + sum(conv(f.amount.amount_minor) for f in p.fees)


# --- entry points -------------------------------------------------------------------------

def evaluate(policy: Policy, state: PolicyState, proposal: Any, now: datetime) -> Decision:
    """May the agent make this hire? Never raises."""
    try:
        return _evaluate_hire(policy, state, proposal, now)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything unexpected
        return make_decision("R0", error=type(exc).__name__)


def evaluate_message(policy: Policy, state: PolicyState, message: Any, now: datetime) -> Decision:
    """May the agent send this message? Never raises."""
    try:
        return _evaluate_message(policy, state, message, now)
    except Exception as exc:  # noqa: BLE001
        return make_decision("R0", error=type(exc).__name__)


def recheck_before_release(policy: Policy, state: PolicyState, now: datetime) -> Decision:
    """Re-run the rules that can change between approval and payment. Never raises.

    Between evaluate() saying allow and the escrow actually holding money, the user may revoke
    the policy, the policy may expire, the freelancer may change the price, or another order
    may use up the budget. Any of those -> deny, and the money does not move.
    """
    try:
        return _recheck(policy, state, now)
    except Exception as exc:  # noqa: BLE001
        return make_decision("R0", error=type(exc).__name__)


def verify_mandate(signed: SignedMandate, trusted_user_pubkey: str) -> Decision:
    """Is the mandate signed by the user's trusted key? Never raises."""
    try:
        policy_id = signed.mandate.policy.policy_id
        if mandate_signature_ok(signed, trusted_user_pubkey):
            return make_decision("OK_MANDATE", policy_id=policy_id)
        return make_decision("R17", policy_id=policy_id)
    except Exception as exc:  # noqa: BLE001
        return make_decision("R0", error=type(exc).__name__)


# --- rules shared by every entry point ------------------------------------------------------

def _time_and_revocation(policy: Policy, state: PolicyState, now: datetime) -> Optional[Decision]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    # R7 expiry: valid from issued_at (inclusive) until expires_at (exclusive).
    if not (policy.issued_at <= now < policy.expires_at):
        return make_decision("R7", now=_iso(now), issued_at=_iso(policy.issued_at), expires_at=_iso(policy.expires_at))
    # R8 revocation: revoked from revoked_at on, or superseded by a newer version.
    revoked = state.revoked_at is not None and now >= state.revoked_at
    superseded = state.latest_policy_version is not None and state.latest_policy_version > policy.version
    if revoked or superseded:
        return make_decision("R8", policy_version=policy.version,
                             revoked_at=_iso(state.revoked_at) if state.revoked_at else None,
                             latest_version=state.latest_policy_version)
    return None


# --- hires ------------------------------------------------------------------------------------

def _evaluate_hire(policy: Policy, state: PolicyState, proposal: Any, now: datetime) -> Decision:
    if not isinstance(proposal, HireRequest):
        try:
            proposal = HireRequest.model_validate(proposal)
        except ValidationError as exc:
            return make_decision("R0", error=f"{exc.error_count()} validation error(s)")
    req: HireRequest = proposal
    p, profile, quote = req.proposal, req.profile, req.quote
    budget_ccy = policy.budget_cap.currency

    if (d := _time_and_revocation(policy, state, now)):
        return d

    # R14: proposal, profile and quote must describe the same freelancer, and the price must be positive.
    problem = _inconsistency(p, profile, quote)
    if problem:
        return make_decision("R14", problem=problem)

    # R2, R3: where and what.
    if p.platform not in policy.allowed_platforms:
        return make_decision("R2", platform=p.platform)
    if p.category not in policy.allowed_categories or p.category not in profile.categories:
        return make_decision("R3", category=p.category)

    # R4: eligibility, from the marketplace's own profile record.
    failures = eligibility_failures(profile, policy)
    if failures:
        f = failures[0]
        return make_decision("R4", freelancer=p.freelancer_id, check=f.check, value=f.value, required=f.required)

    # R6: never pay outside the platform when the policy forbids it.
    if policy.forbid_offplatform and p.payment_channel == "off_platform":
        return make_decision("R6", channel=p.payment_channel)

    # R15: fees and milestones in the price currency; price convertible into the budget currency.
    problem = _currency_problem(p, budget_ccy)
    if problem:
        return make_decision("R15", problem=problem)

    # R5: milestones must add up to the price; milestone_only needs at least two.
    ms_sum = sum(m.amount.amount_minor for m in p.milestones)
    if (p.milestones and ms_sum != p.price.amount_minor) or (policy.milestone_only and len(p.milestones) < 2):
        return make_decision("R5", milestones=len(p.milestones), milestone_sum=ms_sum, price=p.price.amount_minor)

    # R13: the price must be the one HKD price the freelancer wrote, or a human must look.
    parsed = parse_quote(quote.text)
    if p.price.currency != "HKD" or parsed.status != "ok" or parsed.amount_minor != p.price.amount_minor:
        return make_decision("R13", freelancer=p.freelancer_id, price=p.price.amount_minor,
                             found=",".join(str(a) for a in parsed.found) or "none",
                             escalation_expires_at=_iso(escalation_expires_at(now, policy)))

    # R11: number of hires under this policy.
    if len(state.order_times) >= policy.max_hires:
        return make_decision("R11", hires=len(state.order_times), max_hires=policy.max_hires)

    # R9: velocity, at most N orders in any rolling window of M minutes.
    if (d := _velocity(policy, state, now)):
        return d

    # R1: committed + this total must be <= cap. Exactly the cap is allowed.
    total = proposal_total_minor(p)
    cap = policy.budget_cap.amount_minor
    if state.committed_minor + total > cap:
        return make_decision("R1", cap=cap, total=total, committed=state.committed_minor)

    # R12: above the auto-approve limit, a human decides.
    limit = policy.auto_approve_limit
    if limit is not None and total > limit.amount_minor:
        return make_decision("R12", total=total, limit=limit.amount_minor,
                             escalation_expires_at=_iso(escalation_expires_at(now, policy)))

    return make_decision("OK_HIRE", freelancer=p.freelancer_id, total=total, committed=state.committed_minor, cap=cap)


def _velocity(policy: Policy, state: PolicyState, now: datetime) -> Optional[Decision]:
    window_start = now - timedelta(minutes=policy.velocity.window_minutes)
    recent = sum(1 for t in state.order_times if window_start < t <= now)
    if recent >= policy.velocity.max_orders:
        return make_decision("R9", orders=recent, window_minutes=policy.velocity.window_minutes,
                             max_orders=policy.velocity.max_orders)
    return None


def _inconsistency(p: HireProposal, profile: Profile, quote: Message) -> Optional[str]:
    if profile.freelancer_id != p.freelancer_id or profile.platform != p.platform:
        return "profile does not match proposal"
    if quote.freelancer_id != p.freelancer_id or quote.platform != p.platform:
        return "quote does not match proposal"
    if quote.direction != "inbound":
        return "quote is not an inbound message"
    if p.price.amount_minor <= 0:
        return "price is not positive"
    return None


def _currency_problem(p: HireProposal, budget_ccy: str) -> Optional[str]:
    if any(f.amount.currency != p.price.currency for f in p.fees):
        return "fee currency differs from price currency"
    if any(m.amount.currency != p.price.currency for m in p.milestones):
        return "milestone currency differs from price currency"
    if p.fx is None and p.price.currency != budget_ccy:
        return f"price in {p.price.currency} but budget in {budget_ccy} and no FX quote"
    if p.fx is not None and (p.fx.from_currency != p.price.currency or p.fx.to_currency != budget_ccy):
        return f"FX {p.fx.from_currency}->{p.fx.to_currency} does not convert {p.price.currency} into {budget_ccy}"
    return None


# --- messages -----------------------------------------------------------------------------------

def _evaluate_message(policy: Policy, state: PolicyState, message: Any, now: datetime) -> Decision:
    if not isinstance(message, Message):
        try:
            message = Message.model_validate(message)
        except ValidationError as exc:
            return make_decision("R0", error=f"{exc.error_count()} validation error(s)")
    if (d := _time_and_revocation(policy, state, now)):
        return d
    if message.platform not in policy.allowed_platforms:
        return make_decision("R2", platform=message.platform)
    fid = message.freelancer_id
    if fid in policy.blocked_freelancers:
        return make_decision("R4", freelancer=fid, check="blocked", value=True, required=False)
    sent = state.messages_sent.get(fid, 0)
    if message.direction != "outbound":
        return make_decision("R16", freelancer=fid, problem="only outbound messages can be sent")
    if len(message.text) > policy.max_message_chars:
        return make_decision("R16", freelancer=fid, problem=f"{len(message.text)} chars > {policy.max_message_chars}")
    if sent >= policy.max_messages_per_freelancer:
        return make_decision("R16", freelancer=fid, problem=f"{sent} messages already sent, max {policy.max_messages_per_freelancer}")
    return make_decision("OK_MESSAGE", freelancer=fid, sent=sent + 1, max_messages=policy.max_messages_per_freelancer)


# --- time-of-check vs time-of-use -----------------------------------------------------------------

def _recheck(policy: Policy, state: PolicyState, now: datetime) -> Decision:
    pending = state.pending
    if pending is None:
        return make_decision("R0", error="no approved hire is pending")
    if (d := _time_and_revocation(policy, state, now)):
        return d
    approved = pending.proposal.price.amount_minor
    if pending.current_price_minor != approved:
        return make_decision("R10", approved_price=approved, current_price=pending.current_price_minor)
    cap = policy.budget_cap.amount_minor
    if state.committed_minor + pending.approved_total_minor > cap:
        return make_decision("R1", cap=cap, total=pending.approved_total_minor, committed=state.committed_minor)
    return make_decision("OK_RELEASE", proposal=pending.proposal.proposal_id, total=pending.approved_total_minor,
                         committed=state.committed_minor, cap=cap)


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")
