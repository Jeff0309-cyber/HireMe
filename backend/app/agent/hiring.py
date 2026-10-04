"""The vision-to-hire flow, one step per user action.

  draft(vision)      plain language -> draft Brief (nothing is registered yet)
  register(form)     user-reviewed brief -> signed Mandate; the chain log starts here
  search()           browse GigMarket (SIMULATED), assess every gig, message the best ones,
                     propose one hire, and pre-check it with the gate
  confirm()          the USER confirms -> gate evaluate() -> money held in SIMULATED escrow
  deliver()          the seller delivers (SIMULATED) -> file type + word count check
  release()          the USER approves -> recheck_before_release() -> money released

The agent never pays on its own: money moves only in confirm() and release(), both triggered by
the user and both checked by the gate. Flags from hostile text can only make the agent more
cautious (skip a seller); they never cause a payment.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from nacl.signing import SigningKey
from pydantic import BaseModel, ConfigDict, Field

from app.agent.brief import Brief, draft_brief
from app.agent.clarify import Clarifier
from app.agent.messaging import flag_inbound, guard_outbound
from app.contracts import Decision, HireProposal, Mandate, Message, Milestone, Money, Policy, SignedMandate
from app.escrow import compute_fee
from app.escrow.escrow import DeliverableSpec, Escrow
from app.ledger import ChainLog
from app.llm import LLMClient, agent_model
from app.marketplace.access import CallRecord, GigListing, LocalMarketplace, MarketplaceError
from app.marketplace.catalog import CATEGORIES, PLATFORM, PLATFORM_NAME
from app.observed import load_observed
from app.policy import (HireRequest, PolicyState, eligibility_failures, evaluate, evaluate_message, mandate_digest,
                        parse_quote, render_reason, sign_mandate, verify_mandate)

MAX_CONTACTS = 3
STOPWORDS = {"want", "need", "would", "like", "budget", "within", "days", "week", "weeks", "with", "that", "this",
             "your", "from", "have", "make", "some", "about", "into", "upto", "must", "should", "please", "will",
             "someone", "them", "they", "their", "what", "when", "also", "just", "very", "more", "less"}

DELIVERABLE_SPECS: dict[str, DeliverableSpec] = {
    "design": DeliverableSpec((".png", ".svg", ".pdf", ".ai"), 0, 10**9),
    "video": DeliverableSpec((".mp4", ".mov"), 0, 10**9),
    "web": DeliverableSpec((".zip", ".html"), 0, 10**9),
    "audio": DeliverableSpec((".mp3", ".wav"), 0, 10**9),
}


class FlowError(Exception):
    """A step was called out of order or with unusable input. Maps to HTTP 409/422."""


class RegisterForm(BaseModel):
    """What the user signs, after editing the draft."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=120)
    category: str
    budget_hkd: str = Field(pattern=r"^\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?$|^\d+(?:\.\d{1,2})?$")
    deadline_days: int = Field(ge=1, le=365)
    min_words: Optional[int] = Field(default=None, ge=0, le=100_000)
    max_words: Optional[int] = Field(default=None, ge=1, le=100_000)
    min_rating_x100: int = Field(default=450, ge=0, le=500)
    min_completed_orders: int = Field(default=10, ge=0)
    verified_required: bool = True
    milestone_only: bool = False
    principal: str = "demo-user (SIMULATED)"


@dataclass
class Assessment:
    gig_id: str
    title: str
    seller: str
    seller_id: str
    rating_x100: int
    orders: int
    verified: bool
    fit: int
    package: Optional[str]
    price_minor: Optional[int]
    total_minor: Optional[int]
    delivery_days: Optional[int]
    excluded_because: list[str] = field(default_factory=list)
    status: str = "excluded"      # excluded | candidate | contacted_rejected | picked
    rejected_because: Optional[str] = None
    label: str = "SIMULATED"


@dataclass
class ProposalView:
    gig_id: str
    title: str
    seller: str
    package: str
    price_minor: int
    fee_minor: int
    total_minor: int
    delivery_days: int
    milestones: list[int]
    precheck: dict
    why: Optional[str] = None      # the AI agent's own explanation (model text, not a gate reason)


class HiringSession:
    def __init__(self, clock: Callable[[], datetime], user_key: SigningKey, agent_key: SigningKey,
                 llm: Optional[LLMClient] = None, market=None):
        self.clock, self.user_key, self.agent_key, self.llm = clock, user_key, agent_key, llm
        # The marketplace, reached over MCP in the app (LocalMarketplace only in unit tests).
        self.market = market if market is not None else LocalMarketplace()
        self.market.on_call = self._on_market_call
        self.listings: dict[str, GigListing] = {}
        self.user_pubkey = user_key.verify_key.encode().hex()
        self.stage = "new"
        self.brief: Optional[Brief] = None
        self.clarifier: Optional[Clarifier] = None
        self.signed: Optional[SignedMandate] = None
        self.log: Optional[ChainLog] = None
        self.state = PolicyState()
        self.escrow: Optional[Escrow] = None
        self.assessments: list[Assessment] = []
        self.conversation: list[dict] = []
        self.proposal: Optional[ProposalView] = None
        self._request: Optional[HireRequest] = None
        self.last_decision: Optional[dict] = None
        self.observed = load_observed()
        self._n = 0
        self.activity: list[dict] = []          # what the agent is doing, for the UI
        self.no_match_reason: Optional[str] = None
        self.last_inbound: dict[str, Message] = {}

    # --- helpers ---------------------------------------------------------------------------------

    @property
    def policy(self) -> Policy:
        if not self.signed:
            raise FlowError("register the task first")
        return self.signed.mandate.policy

    def _need(self, *stages: str) -> None:
        if self.stage not in stages:
            raise FlowError(f"can't do that now (stage: {self.stage})")

    def _iso(self) -> str:
        return self.clock().isoformat().replace("+00:00", "Z")

    def _decision_out(self, d: Decision) -> dict:
        return {"outcome": d.outcome, "rule_id": d.rule_id, "reason": render_reason(d)}

    def _gate_log(self, event: str, action: dict, d: Decision) -> Decision:
        assert self.log is not None
        self.log.append(event, {"action": action, "details": d.details}, d)
        return d

    def _message(self, fid: str, direction: str, text: str) -> Message:
        self._n += 1
        return Message(message_id=f"msg-{self._n}", platform=PLATFORM, freelancer_id=fid, direction=direction,
                       text=text, untrusted=(direction == "inbound"), sent_at=self.clock())

    def _on_market_call(self, rec: CallRecord) -> None:
        """Every MCP call is shown in the agent feed and written to the signed log."""
        self.activity.append({"kind": "mcp", "server": rec.server, "tool": rec.tool, "ms": rec.ms, "ok": rec.ok,
                              "summary": rec.summary})
        if self.log is not None:
            self.log.append("mcp_call", {"server": rec.server, "tool": rec.tool, "args": rec.args, "ms": rec.ms,
                                         "ok": rec.ok, "summary": rec.summary})

    def listing(self, gig_id: str) -> GigListing:
        if gig_id not in self.listings:
            self.listings[gig_id] = self.market.get(gig_id)
        return self.listings[gig_id]

    def ask_seller(self, g: GigListing, package: str, text: str) -> Optional[tuple[Message, tuple[str, ...]]]:
        """Send one message (guard + gate) and read the reply over the marketplace connection."""
        if not self._send(g, text):
            return None
        try:
            reply = self.market.message(g.gig_id, package, text)
        except MarketplaceError as exc:
            self.log.append("message_failed", {"gig_id": g.gig_id, "error": str(exc)[:160]})
            return None
        return self._receive(g, reply)

    def _send(self, g: GigListing, text: str) -> bool:
        guard = guard_outbound(text)
        if not guard.ok:
            self.log.append("message_blocked", {"freelancer_id": g.seller.freelancer_id, "text": text,
                                                "violations": list(guard.violations)})
            return False
        msg = self._message(g.seller.freelancer_id, "outbound", text)
        d = self._gate_log("gate_decision", {"kind": "send_message", "message": msg.model_dump(mode="json")},
                           evaluate_message(self.policy, self.state, msg, self.clock()))
        if d.outcome != "allow":
            return False
        self.state.messages_sent[g.seller.freelancer_id] = self.state.messages_sent.get(g.seller.freelancer_id, 0) + 1
        self.conversation.append({"from": "agent", "gig_id": g.gig_id, "text": text, "flags": []})
        return True

    def _receive(self, g: GigListing, text: str) -> tuple[Message, tuple[str, ...]]:
        msg = self._message(g.seller.freelancer_id, "inbound", text)
        self.log.append("message_received", {"message": msg.model_dump(mode="json"), "gig_id": g.gig_id,
                                             "label": "SIMULATED"})
        flags = flag_inbound(text)
        if flags:
            self.log.append("message_flagged", {"message_id": msg.message_id, "freelancer_id": g.seller.freelancer_id,
                                                "flags": list(flags), "action_taken": "none"})
        self.conversation.append({"from": "seller", "gig_id": g.gig_id, "seller": g.seller.display_name,
                                  "text": text, "flags": list(flags), "label": "SIMULATED"})
        self.last_inbound[g.gig_id] = msg
        return msg, flags

    # --- steps ---------------------------------------------------------------------------------------

    # --- 1. understand the idea: a short clarifying chat, then a draft brief ------------------------

    def start_chat(self, vision: str) -> None:
        """The user pasted their idea. The AI asks questions until it understands, then drafts."""
        self._need("new", "clarifying", "drafted")
        if not vision.strip():
            raise FlowError("please describe what you want done")
        self.clarifier = Clarifier(vision, self.llm)
        self.clarifier.start()
        self._after_turn()

    def answer(self, text: str) -> None:
        self._need("clarifying")
        try:
            self.clarifier.answer(text)
        except ValueError as exc:
            raise FlowError(str(exc)) from exc
        self._after_turn()

    def skip_questions(self) -> None:
        self._need("clarifying")
        self.clarifier.finish_now()
        self._after_turn()

    def _after_turn(self) -> None:
        if self.clarifier.ready:
            self.brief = self.clarifier.brief
            self.stage = "drafted"
        else:
            self.stage = "clarifying"

    def draft(self, vision: str) -> Brief:
        self._need("new", "drafted")
        try:
            self.brief = draft_brief(vision, self.llm)
        except ValueError as exc:
            raise FlowError(str(exc)) from exc
        self.stage = "drafted"
        return self.brief

    def register(self, form: RegisterForm) -> SignedMandate:
        self._need("drafted", "new")
        if form.category not in CATEGORIES:
            raise FlowError(f"unknown category {form.category!r}")
        if form.min_words and form.max_words and form.min_words > form.max_words:
            raise FlowError("minimum words is larger than maximum words")
        from app.money import decimal_str_to_minor
        budget = decimal_str_to_minor(form.budget_hkd)
        if budget <= 0:
            raise FlowError("the budget must be more than zero")
        now = self.clock()
        vision = self.brief.vision if self.brief else form.title
        policy = Policy(
            policy_id=f"p-{now.strftime('%Y%m%d%H%M%S')}", version=1, issued_at=now,
            expires_at=now + timedelta(days=form.deadline_days + 14),
            budget_cap=Money(amount_minor=budget), auto_approve_limit=Money(amount_minor=budget), max_hires=1,
            allowed_platforms=[PLATFORM], allowed_categories=[form.category],
            eligibility={"min_rating_x100": form.min_rating_x100, "min_completed_orders": form.min_completed_orders,
                         "verified_required": form.verified_required},
            milestone_only=form.milestone_only, forbid_offplatform=True,
            velocity={"max_orders": 3, "window_minutes": 60},
            max_messages_per_freelancer=3, max_message_chars=800, escalation_ttl_seconds=86_400,
        )
        mandate = Mandate(principal=form.principal, task=f"{form.title}\n\n{vision}"[:1000], category=form.category,
                          policy=policy)
        self.signed = sign_mandate(mandate, self.user_key)
        self.log = ChainLog(self.agent_key, self._iso, policy_hash=mandate_digest(mandate))
        self.log.append("task_registered", {
            "title": form.title, "vision": vision, "category": form.category, "deadline_days": form.deadline_days,
            "min_words": form.min_words, "max_words": form.max_words,
            "brief_source": self.brief.source if self.brief else "form", "label": "SIMULATED",
        })
        d = verify_mandate(self.signed, self.user_pubkey)
        self._gate_log("gate_decision", {"kind": "verify_mandate"}, d)
        if d.outcome != "allow":
            raise FlowError("the mandate signature did not verify")
        kind = CATEGORIES[form.category].deliverable
        spec = DELIVERABLE_SPECS.get(kind) or DeliverableSpec((".md", ".txt", ".docx"), form.min_words or 1,
                                                              form.max_words or 100_000)
        self.escrow = Escrow(policy, self.state, self.log, self.clock, spec)
        self.form = form
        self.stage = "registered"
        return self.signed

    # --- search: Claude agent when a model is configured, deterministic rules otherwise --------------

    @property
    def uses_agent(self) -> bool:
        return self.llm is not None and self.llm.mode in ("live", "offline")

    def begin_search(self) -> None:
        """Mark the session as searching (the API then runs search() in the background)."""
        self._need("registered", "no_match")
        self.stage = "searching"

    def search(self) -> list[Assessment]:
        if self.stage != "searching":
            self.begin_search()
        form = self.form
        limit = self._spend_limit()
        self.log.append("search_started", {"platform": PLATFORM, "platform_name": PLATFORM_NAME, "category": form.category,
                                           "max_total_minor": limit, "deadline_days": form.deadline_days,
                                           "agent": self.llm.provider if self.uses_agent else "rules",
                                           "marketplace_connection": self.market.transport, "label": "SIMULATED"})
        self.assessments, self.conversation, self.proposal, self.activity = [], [], None, []
        self.no_match_reason = None
        try:
            if self.uses_agent:
                if self.llm.provider == "gemini":
                    from app.agent.gemini_agent import GeminiHiringAgent as Agent
                else:
                    from app.agent.claude_agent import ClaudeHiringAgent as Agent
                outcome = Agent(self).run()
                if outcome == "error":
                    self.activity.append({"kind": "status", "text": "Falling back to GigGuard's rule-based search."})
                    self.assessments, self.conversation, self.proposal = [], [], None
                    self._search_rules(limit)
            else:
                self._search_rules(limit)
        except MarketplaceError as exc:
            self.no_match_reason = f"Couldn't reach the marketplace ({exc}). Nothing was paid."
            self.log.append("marketplace_error", {"error": str(exc)[:200]})
        except Exception:
            self.stage = "no_match"
            self.no_match_reason = "Something went wrong during the search. Nothing was paid."
            self.log.append("no_match", {"code": "SEARCH_ERROR"})
            raise
        if self.proposal is None:
            self.log.append("no_match", {"candidates": sum(a.status != "excluded" for a in self.assessments)})
            self.no_match_reason = self.no_match_reason or "No seller fits your rules, budget and deadline."
            self.stage = "no_match"
        else:
            self.stage = "proposed"
        return self.assessments

    def _spend_limit(self) -> int:
        policy = self.policy
        limit = policy.budget_cap.amount_minor - self.state.committed_minor
        if policy.auto_approve_limit:
            limit = min(limit, policy.auto_approve_limit.amount_minor)
        return limit

    def _keywords(self) -> set[str]:
        text = self.form.title + " " + (self.brief.vision if self.brief else "")
        return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3 and w not in STOPWORDS}

    def assess(self, g: GigListing, limit: int) -> Assessment:
        """GigGuard's own checks on one gig (eligibility, red flags, budget, deadline). Logged."""
        form, policy = self.form, self.policy
        words = set(re.findall(r"[a-z]+", (g.title + " " + " ".join(g.tags)).lower()))
        a = Assessment(gig_id=g.gig_id, title=g.title, seller=g.seller.display_name, seller_id=g.seller.freelancer_id,
                       rating_x100=g.seller.rating_x100, orders=g.seller.completed_orders, verified=g.seller.verified,
                       fit=len(self._keywords() & words), package=None, price_minor=None, total_minor=None,
                       delivery_days=None)
        a.excluded_because += [f.code for f in eligibility_failures(g.seller, policy)]
        a.excluded_because += [f"FLAG.{f}" for f in flag_inbound(g.description)]
        fitting = [p for p in g.packages if p.delivery_days <= form.deadline_days
                   and p.price_minor + compute_fee(p.price_minor, self.observed).fee_minor <= limit]
        if fitting:
            p = min(fitting, key=lambda p: p.price_minor)
            a.package, a.price_minor, a.delivery_days = p.name, p.price_minor, p.delivery_days
            a.total_minor = p.price_minor + compute_fee(p.price_minor, self.observed).fee_minor
        elif all(p.delivery_days > form.deadline_days for p in g.packages):
            a.excluded_because.append("TOO_SLOW")
        else:
            a.excluded_because.append("OVER_BUDGET")
        a.status = "excluded" if a.excluded_because else "candidate"
        self.assessments.append(a)
        self.listings[g.gig_id] = g
        self.log.append("gig_assessed", {k: v for k, v in asdict(a).items() if k not in ("title",)})
        return a

    def _search_rules(self, limit: int) -> None:
        """Deterministic search: rank by keyword fit, then rating, then price; message the top 3."""
        form = self.form
        for g in self.market.search(form.category):
            self.assess(g, limit)
        ranked = sorted((a for a in self.assessments if a.status == "candidate"),
                        key=lambda a: (-a.fit, -a.rating_x100, a.total_minor, a.gig_id))
        for a in ranked[:MAX_CONTACTS]:
            g = self.listing(a.gig_id)
            first = g.seller.display_name.split()[0]
            intro = (f"Hi {first}, I found your gig \"{g.title}\". Could you do the {a.package} package for my client "
                     f"within {form.deadline_days} days? The job: {form.title.rstrip('.!? ')}. Please reply with the total price in HKD.")
            got = self.ask_seller(g, a.package, intro)
            if got is None:
                fallback = (f"Hi {first}, could you do the {a.package} package within {form.deadline_days} days? "
                            "Please reply with the total price in HKD.")
                got = self.ask_seller(g, a.package, fallback)
                if got is None:
                    a.status, a.rejected_because = "contacted_rejected", "COULD_NOT_MESSAGE"
                    continue
            reply, flags = got
            q = parse_quote(reply.text)
            if flags:
                a.rejected_because = "FLAGGED:" + ",".join(flags)
            elif q.status != "ok":
                a.rejected_because = "UNCLEAR_PRICE" if q.status == "ambiguous" else "NO_PRICE"
            elif q.amount_minor != a.price_minor:
                a.rejected_because = "PRICE_DIFFERS_FROM_LISTING"
            if a.rejected_because:
                a.status = "contacted_rejected"
                self.log.append("seller_skipped", {"gig_id": g.gig_id, "freelancer_id": g.seller.freelancer_id,
                                                   "code": a.rejected_because})
                continue
            a.status = "picked"
            self._propose(g, a, reply)
            break

    def _propose(self, g: GigListing, a: Assessment, quote: Message) -> None:
        fee = compute_fee(a.price_minor, self.observed)
        milestones: list[Milestone] = []
        if self.policy.milestone_only:
            half = a.price_minor // 2
            milestones = [Milestone(milestone_id="m1", title="First half", amount=Money(amount_minor=half)),
                          Milestone(milestone_id="m2", title="On delivery", amount=Money(amount_minor=a.price_minor - half))]
        proposal = HireProposal(proposal_id=f"hp-{g.gig_id}", platform=PLATFORM, freelancer_id=g.seller.freelancer_id,
                                category=self.form.category, price=Money(amount_minor=a.price_minor),
                                fees=[fee.as_fee()], milestones=milestones, payment_channel="platform_escrow")
        self._request = HireRequest(proposal=proposal, profile=g.seller, quote=quote)
        # Dry run: what will the gate say if the user confirms? Nothing is paid here.
        d = self._gate_log("gate_precheck", {"kind": "hire_precheck", "proposal": proposal.model_dump(mode="json")},
                           evaluate(self.policy, self.state, self._request, self.clock()))
        self.proposal = ProposalView(gig_id=g.gig_id, title=g.title, seller=g.seller.display_name, package=a.package,
                                     price_minor=a.price_minor, fee_minor=fee.fee_minor,
                                     total_minor=a.price_minor + fee.fee_minor, delivery_days=a.delivery_days,
                                     milestones=[m.amount.amount_minor for m in milestones],
                                     precheck=self._decision_out(d))
        self.log.append("hire_proposed", {"gig_id": g.gig_id, "freelancer_id": g.seller.freelancer_id,
                                          "package": a.package, "total_minor": self.proposal.total_minor,
                                          "awaiting": "user_confirmation"})

    def confirm(self) -> dict:
        """The user said yes. Only now does the gate decide and money move into escrow."""
        self._need("proposed")
        self.log.append("user_confirmed", {"gig_id": self.proposal.gig_id, "total_minor": self.proposal.total_minor})
        self.escrow.create("order-1", self._request, idempotency_key="create-1")
        d = self.escrow.fund("order-1", idempotency_key="fund-1")
        self.last_decision = self._decision_out(d)
        self.stage = "funded" if d.outcome == "allow" else ("escalated" if d.outcome == "escalate" else "denied")
        return self.last_decision

    def deliver(self) -> dict:
        """SIMULATED: the seller uploads the work."""
        self._need("funded")
        g = self.listing(self.proposal.gig_id)
        words = ((self.form.min_words or 150) + (self.form.max_words or 250)) // 2
        try:
            filename, content, note = self.market.delivery(g.gig_id, words)
        except MarketplaceError as exc:
            raise FlowError(f"couldn't fetch the delivery from the marketplace: {exc}") from exc
        self._receive(g, note)
        check = self.escrow.deliver("order-1", filename, content, idempotency_key="deliver-1")
        self.stage = "delivered" if check.ok else "held"
        return {"ok": check.ok, "filename": filename, "file_type": check.file_type, "words": check.words,
                "problem": check.problem}

    def release(self) -> dict:
        """The user approved the work. The gate re-checks before the money is released."""
        self._need("delivered")
        self.log.append("user_approved_delivery", {"order_id": "order-1"})
        d = self.escrow.release("order-1", idempotency_key="release-1")
        self.last_decision = self._decision_out(d)
        self.stage = "released" if d.outcome == "allow" else "held"
        return self.last_decision

    # --- view ------------------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        order = self.escrow.orders.get("order-1") if self.escrow else None
        return {
            "stage": self.stage,
            "brief": self.brief.model_dump() if self.brief else None,
            "chat": [t.model_dump() for t in self.clarifier.turns] if self.clarifier else [],
            "mandate": None if not self.signed else {
                "signed_mandate": self.signed.model_dump(mode="json"), "policy_hash": mandate_digest(self.signed.mandate),
                "trusted_user_pubkey": self.user_pubkey},
            "assessments": [asdict(a) for a in self.assessments],
            "conversation": self.conversation,
            "proposal": asdict(self.proposal) if self.proposal else None,
            "order": None if not order else {"order_id": order.order_id, "state": order.state, "history": order.history,
                                             "total_minor": order.total_minor},
            "escrow": None if not self.escrow else {"held_minor": self.escrow.held_minor(),
                                                    "released_minor": self.escrow.released_minor(),
                                                    "fees_minor": self.escrow.fees_minor()},
            "last_decision": self.last_decision,
            "platform": {"id": PLATFORM, "name": PLATFORM_NAME, "label": "SIMULATED"},
            "form": self.form.model_dump() if self.signed else None,
            "activity": list(self.activity),
            "no_match_reason": self.no_match_reason,
            "ai": {"mode": self.llm.mode if self.llm else "stub",
                   "provider": self.llm.provider if self.llm else "none",
                   "agent": (self.llm.provider if self.uses_agent else "rules"),
                   "model": self.llm.model_for_agent() if self.llm else agent_model()},
            "connections": [self.market.status()],
        }

