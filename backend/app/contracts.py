"""Shared data contracts. Shapes only, no business logic.

These models say what a valid piece of data looks like. They do not decide whether an action
is allowed; that is the policy gate's job. Validators here only reject data that is malformed
or self-contradictory (a float price, a naive timestamp, an inbound message marked trusted).

Conventions used everywhere:
- Money is an int of minor units (cents) plus an ISO 4217 currency code. Never a float.
- Ratings are ints x100 (4.85 stars -> 485) so no floats leak in anywhere.
- FX rates are ints in micros (rate x 1,000,000).
- Timestamps must carry a timezone.
- Every model forbids unknown fields and is immutable once built.

See docs/CONTRACTS.md for a plain-English explanation of each model.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

# --- small reusable field types -------------------------------------------------------

Id = Annotated[StrictStr, Field(min_length=1, max_length=128)]
PlatformId = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
CurrencyCode = Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
MinorUnits = Annotated[StrictInt, Field(ge=0)]
RatingX100 = Annotated[StrictInt, Field(ge=0, le=500)]
PositiveInt = Annotated[StrictInt, Field(ge=1)]
Hex64 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Hex128 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{128}$")]
# Detail values: no floats, so every decision serialises the same way every time.
DetailValue = Union[StrictStr, StrictInt, StrictBool, None]


class Contract(BaseModel):
    """Base for every contract: unknown fields rejected, instances immutable."""
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- money ----------------------------------------------------------------------------

class Money(Contract):
    amount_minor: MinorUnits
    currency: CurrencyCode = "HKD"


class FxQuote(Contract):
    """A conversion rate, taken from data/observed.json (source_id points at the entry)."""
    from_currency: CurrencyCode
    to_currency: CurrencyCode
    rate_micros: PositiveInt              # 1 from_currency = rate_micros / 1e6 to_currency
    source_id: Id
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _different_currencies(self) -> "FxQuote":
        if self.from_currency == self.to_currency:
            raise ValueError("an FX quote needs two different currencies")
        return self


class Fee(Contract):
    """One fee line, e.g. a marketplace service fee. source_id points into data/observed.json."""
    name: Id
    amount: Money
    source_id: Id


# --- policy (the user's signed mandate) -----------------------------------------------

class Eligibility(Contract):
    min_rating_x100: RatingX100 = 0
    min_completed_orders: Annotated[StrictInt, Field(ge=0)] = 0
    verified_required: StrictBool = False


class Velocity(Contract):
    """At most max_orders orders in any rolling window of window_minutes."""
    max_orders: PositiveInt
    window_minutes: PositiveInt


class Policy(Contract):
    policy_id: Id
    version: PositiveInt
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    budget_cap: Money                         # everything spent under this policy, fees included
    auto_approve_limit: Optional[Money] = None  # a single hire above this needs a human
    max_hires: PositiveInt = 1
    allowed_platforms: Annotated[list[PlatformId], Field(min_length=1)]
    allowed_categories: Annotated[list[Id], Field(min_length=1)]
    blocked_freelancers: list[Id] = Field(default_factory=list)
    eligibility: Eligibility = Eligibility()
    milestone_only: StrictBool = False
    forbid_offplatform: StrictBool = True
    velocity: Velocity
    max_messages_per_freelancer: PositiveInt = 3
    max_message_chars: PositiveInt = 800
    escalation_ttl_seconds: PositiveInt

    @model_validator(mode="after")
    def _self_consistent(self) -> "Policy":
        if self.issued_at >= self.expires_at:
            raise ValueError("issued_at must be before expires_at")
        if self.auto_approve_limit and self.auto_approve_limit.currency != self.budget_cap.currency:
            raise ValueError("auto_approve_limit must use the budget_cap currency")
        return self


class Mandate(Contract):
    """What the user asks for (task, category) plus the limits (policy). This is what gets signed."""
    principal: Annotated[StrictStr, Field(min_length=1)]
    task: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    category: Id
    policy: Policy

    @model_validator(mode="after")
    def _category_allowed(self) -> "Mandate":
        if self.category not in self.policy.allowed_categories:
            raise ValueError("the task category must be one of the policy's allowed_categories")
        return self


class SignedMandate(Contract):
    """A mandate plus the user's Ed25519 signature over its canonical JSON.

    signer_pubkey is informational. The gate verifies against the user key the caller trusts.
    """
    mandate: Mandate
    signer_pubkey: Hex64
    signature: Hex128


# --- marketplace data -----------------------------------------------------------------

class Profile(Contract):
    """A freelancer as a marketplace shows them. Free text is written by the freelancer: UNTRUSTED."""
    platform: PlatformId
    freelancer_id: Id
    display_name: StrictStr
    categories: list[Id] = Field(default_factory=list)
    skills: list[StrictStr] = Field(default_factory=list)
    rating_x100: RatingX100
    completed_orders: Annotated[StrictInt, Field(ge=0)]
    verified: StrictBool
    profile_text: StrictStr = ""
    untrusted: Literal[True] = True
    label: Literal["SIMULATED"] = "SIMULATED"


class Scorecard(Contract):
    """How the agent rated one candidate. Filled in by later code; here it is only a shape."""
    platform: PlatformId
    freelancer_id: Id
    fit_score: Annotated[StrictInt, Field(ge=0, le=100)]
    eligible: StrictBool
    eligibility_failures: list[Id] = Field(default_factory=list)   # rule ids
    quoted_price: Optional[Money] = None
    total_cost: Optional[Money] = None
    rank: Optional[PositiveInt] = None

    @model_validator(mode="after")
    def _failures_match_flag(self) -> "Scorecard":
        if self.eligible == bool(self.eligibility_failures):
            raise ValueError("eligible must be true exactly when eligibility_failures is empty")
        return self


class Message(Contract):
    message_id: Id
    platform: PlatformId
    freelancer_id: Id
    direction: Literal["outbound", "inbound"]
    text: StrictStr
    untrusted: StrictBool
    sent_at: AwareDatetime

    @model_validator(mode="after")
    def _inbound_is_untrusted(self) -> "Message":
        if self.direction == "inbound" and not self.untrusted:
            raise ValueError("inbound messages are always untrusted")
        return self


class Milestone(Contract):
    milestone_id: Id
    title: StrictStr
    amount: Money
    due_at: Optional[AwareDatetime] = None
    status: Literal["pending", "funded", "released", "refunded"] = "pending"


class HireProposal(Contract):
    """What the agent wants to do. The policy gate decides whether it may."""
    proposal_id: Id
    platform: PlatformId
    freelancer_id: Id
    category: Id
    price: Money
    fees: list[Fee] = Field(default_factory=list)
    fx: Optional[FxQuote] = None
    milestones: list[Milestone] = Field(default_factory=list)
    payment_channel: Literal["platform_escrow", "platform_direct", "off_platform"]


# --- decisions and records ------------------------------------------------------------

class Decision(Contract):
    outcome: Literal["allow", "deny", "escalate"]
    rule_id: Id
    details: dict[StrictStr, DetailValue] = Field(default_factory=dict)


class Order(Contract):
    order_id: Id
    proposal_id: Id
    platform: PlatformId
    freelancer_id: Id
    total: Money
    milestones: list[Milestone] = Field(default_factory=list)
    status: Literal["created", "in_progress", "delivered", "completed", "cancelled", "disputed"] = "created"
    created_at: AwareDatetime
    label: Literal["SIMULATED"] = "SIMULATED"


class LedgerEntry(Contract):
    """One SIMULATED money movement in escrow. Not the audit log; see LogEntry."""
    entry_id: Id
    order_id: Id
    milestone_id: Optional[Id] = None
    kind: Literal["hold", "release", "refund", "fee"]
    amount: Money
    at: AwareDatetime
    label: Literal["SIMULATED"] = "SIMULATED"


class LogEntry(Contract):
    """One line of the signed, hash-chained audit log (the format verifier/verify_log.py checks)."""
    seq: Annotated[StrictInt, Field(ge=0)]
    timestamp: AwareDatetime
    prev_hash: Hex64
    policy_hash: Hex64
    event_type: Id
    inputs: dict[StrictStr, Any]
    rule_id: Optional[Id] = None
    decision: Optional[Literal["allow", "deny", "escalate"]] = None
    reason: Optional[StrictStr] = None
    entry_hash: Hex64
    signature: Hex128

    @model_validator(mode="after")
    def _check(self) -> "LogEntry":
        _reject_floats(self.inputs)
        if len({self.rule_id is None, self.decision is None, self.reason is None}) != 1:
            raise ValueError("rule_id, decision and reason are set together (a gate decision) or not at all")
        return self


def _reject_floats(obj: Any) -> None:
    if isinstance(obj, float):
        raise ValueError("log inputs may not contain floats")
    if isinstance(obj, dict):
        for v in obj.values():
            _reject_floats(v)
    elif isinstance(obj, list):
        for v in obj:
            _reject_floats(v)


ALL_CONTRACTS: tuple[type[Contract], ...] = (
    Money, FxQuote, Fee, Eligibility, Velocity, Policy, Mandate, SignedMandate, Profile, Scorecard, Message, Milestone,
    HireProposal, Decision, Order, LedgerEntry, LogEntry,
)
