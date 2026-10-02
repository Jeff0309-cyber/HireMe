"""The hiring agent.

Flow (each step is logged):
  1. search the marketplace for the mandate's category
  0. verify the user's signature on the mandate; stop if it fails
  2. build a Scorecard per candidate: eligibility (same function the gate uses) + LLM fit score
  3. shortlist the best eligible fits and message them (via the gate)
  4. read each reply's price DETERMINISTICALLY (no LLM touches money)
  5. if a quote is above what can be auto-approved, send one templated counter-offer
  6. rank by total cost, build HireProposals (split into milestones if the policy says so)
  7. try to hire cheapest first: gate check, confirm with the freelancer, gate re-check, fund.
     Stop at the first allow or escalate; on deny (incl. a price change or revocation mid-way)
     move to the next offer

The LLM may only produce a fit score and a friendly first message. Prices, fees, ranking,
milestones and the hire decision are plain code plus the policy gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from app.agent.executor import GatedExecutor
from app.agent.prompts import DRAFT_SYSTEM, SCORE_SYSTEM, draft_prompt, parse_fit, score_prompt
from app.contracts import HireProposal, Milestone, Money, Profile, Scorecard
from app.escrow import FEE_SOURCE_ID, FeeQuote, compute_fee
from app.llm import LLMClient, LLMError, agent_model, fast_model
from app.money import apply_bps, format_hkd
from app.observed import ObservedValue
from app.policy import eligibility_failures, mandate_digest, parse_quote

SHORTLIST_SIZE = 3
MIN_FIT_SCORE = 50          # scorecard fit is 0-100; the LLM answers 0-10 and we multiply by 10
Status = Literal["hired", "escalated", "no_hire"]


@dataclass
class Offer:
    profile: Profile
    fit_score: int
    amount_minor: int
    fee: FeeQuote

    @property
    def total_minor(self) -> int:
        return self.amount_minor + self.fee.fee_minor


@dataclass
class RunSummary:
    status: Status
    hired: Optional[str]
    spent_minor: int


def run_agent(ex: GatedExecutor, llm: LLMClient, observed: dict[str, ObservedValue]) -> RunSummary:
    mandate, policy, log = ex.mandate, ex.policy, ex.log
    log.append("run_started", {
        "signed_mandate": ex.signed.model_dump(mode="json"), "mandate_digest": mandate_digest(mandate),
        "agent_model": agent_model(), "fast_model": fast_model(), "llm_mode": llm.mode,
        "agent_pubkey": log.public_key_hex, "label": "SIMULATED",
    })

    # 0. Refuse to act on a mandate the user did not sign.
    if ex.check_mandate().outcome != "allow":
        log.append("run_finished", {"status": "no_hire", "hired": None, "committed_minor": ex.state.committed_minor})
        return RunSummary(status="no_hire", hired=None, spent_minor=0)

    # 1. Search.
    profiles = ex.marketplace.search(mandate.category)
    log.append("search_results", {"platform": ex.marketplace.name,
                                  "freelancer_ids": [p.freelancer_id for p in profiles], "label": "SIMULATED"})

    # 2. Scorecards. Ineligible candidates are not contacted; LLM failures exclude the candidate.
    scorecards: dict[str, Scorecard] = {}
    for p in profiles:
        failures = [f.code for f in eligibility_failures(p, policy)]
        fit = 0
        if not failures:
            try:
                fit = 10 * parse_fit(llm.complete("score_fit", agent_model(), SCORE_SYSTEM, score_prompt(mandate, p))).fit
            except (LLMError, ValueError) as exc:
                log.append("candidate_excluded", {"freelancer_id": p.freelancer_id, "code": "FIT_SCORE_FAILED",
                                                  "error": type(exc).__name__})
                continue
        card = Scorecard(platform=p.platform, freelancer_id=p.freelancer_id, fit_score=fit,
                         eligible=not failures, eligibility_failures=failures)
        scorecards[p.freelancer_id] = card
        log.append("scorecard", {"scorecard": card.model_dump(mode="json")})

    # 3. Shortlist: best fit first, then id for a stable order.
    by_id = {p.freelancer_id: p for p in profiles}
    shortlist = sorted((c for c in scorecards.values() if c.eligible and c.fit_score >= MIN_FIT_SCORE),
                       key=lambda c: (-c.fit_score, c.freelancer_id))[:SHORTLIST_SIZE]
    log.append("shortlist", {"freelancer_ids": [c.freelancer_id for c in shortlist], "min_fit_score": MIN_FIT_SCORE})

    # 4-5. Message, read quotes, counter once if needed.
    auto_limit = policy.budget_cap.amount_minor - ex.state.committed_minor
    if policy.auto_approve_limit is not None:
        auto_limit = min(auto_limit, policy.auto_approve_limit.amount_minor)
    offers = [o for c in shortlist
              if (o := _negotiate(ex, llm, observed, by_id[c.freelancer_id], c.fit_score, auto_limit))]

    # 6. Rank by total cost and record it on the scorecards.
    offers.sort(key=lambda o: (o.total_minor, -o.fit_score, o.profile.freelancer_id))
    ranked = []
    for rank, o in enumerate(offers, start=1):
        card = scorecards[o.profile.freelancer_id].model_copy(update={
            "quoted_price": Money(amount_minor=o.amount_minor), "total_cost": Money(amount_minor=o.total_minor),
            "rank": rank})
        ranked.append(card.model_dump(mode="json"))
    log.append("offers_ranked", {"scorecards": ranked})

    # 7. Try to hire, cheapest total first.
    status: Status = "no_hire"
    hired: Optional[str] = None
    for o in offers:
        proposal = _proposal(ex, o)
        decision = ex.hire(proposal)
        if decision.outcome == "allow":
            status, hired = "hired", o.profile.freelancer_id
            break
        if decision.outcome == "escalate":
            log.append("escalation_requested", {
                "proposal_id": proposal.proposal_id, "freelancer_id": o.profile.freelancer_id,
                "rule_id": decision.rule_id, "expires_at": decision.details.get("escalation_expires_at")})
            status = "escalated"
            break

    spent = sum(o.total.amount_minor for o in ex.escrow.orders.values())
    summary = RunSummary(status=status, hired=hired, spent_minor=spent)
    log.append("run_finished", {"status": status, "hired": hired, "spent_minor": spent,
                                "committed_minor": ex.state.committed_minor})
    return summary


def _proposal(ex: GatedExecutor, o: Offer) -> HireProposal:
    """Always pay through platform escrow. Split into two milestones when the policy requires it."""
    fid = o.profile.freelancer_id
    milestones: list[Milestone] = []
    if ex.policy.milestone_only:
        first = o.amount_minor // 2
        milestones = [
            Milestone(milestone_id=f"{fid}-m1", title="First half: concepts", amount=Money(amount_minor=first)),
            Milestone(milestone_id=f"{fid}-m2", title="Second half: final files",
                      amount=Money(amount_minor=o.amount_minor - first)),
        ]
    return HireProposal(
        proposal_id=f"hp-{fid}", platform=o.profile.platform, freelancer_id=fid, category=ex.mandate.category,
        price=Money(amount_minor=o.amount_minor), fees=[o.fee.as_fee()], milestones=milestones,
        payment_channel="platform_escrow",
    )


def _negotiate(ex: GatedExecutor, llm: LLMClient, observed: dict[str, ObservedValue],
               p: Profile, fit_score: int, auto_limit: int) -> Optional[Offer]:
    reply = ex.send_message(p.freelancer_id, _draft(ex, llm, p))
    if reply is None:
        return None
    offer = _read_quote(ex, observed, p, fit_score, reply.text)
    if offer is None or offer.total_minor <= auto_limit:
        return offer

    # One counter-offer, written from a fixed template (money text never comes from the LLM).
    target = _max_price_within(auto_limit, observed)
    counter = f"Thanks for the quote. Could you do the whole job for {format_hkd(target)}?"
    reply2 = ex.send_message(p.freelancer_id, counter)
    if reply2 is None:
        return offer
    return _read_quote(ex, observed, p, fit_score, reply2.text) or offer


def _draft(ex: GatedExecutor, llm: LLMClient, p: Profile) -> str:
    """LLM-drafted intro; falls back to a template if the LLM fails or mentions money."""
    fid = p.freelancer_id
    try:
        text = llm.complete("draft_outreach", agent_model(), DRAFT_SYSTEM, draft_prompt(ex.mandate, p)).strip()
        if parse_quote(text).status == "none" and len(text) <= ex.policy.max_message_chars:
            ex.log.append("draft_ready", {"freelancer_id": fid, "source": "llm"})
            return text
        ex.log.append("draft_ready", {"freelancer_id": fid, "source": "template", "code": "LLM_DRAFT_REJECTED"})
    except LLMError as exc:
        ex.log.append("draft_ready", {"freelancer_id": fid, "source": "template", "code": "LLM_ERROR",
                                         "error": type(exc).__name__})
    return f"Hi {p.display_name}, I'm reaching out for a client who needs: {ex.mandate.task}. " \
           "Could you reply with one total price in HKD for the whole job?"


def _read_quote(ex: GatedExecutor, observed: dict[str, ObservedValue], p: Profile,
                fit_score: int, reply_text: str) -> Optional[Offer]:
    q = parse_quote(reply_text)
    ex.log.append("quote_parsed", {"freelancer_id": p.freelancer_id, "status": q.status,
                                      "amount_minor": q.amount_minor, "found": q.found})
    if q.status != "ok" or q.amount_minor is None:
        return None
    return Offer(profile=p, fit_score=fit_score, amount_minor=q.amount_minor, fee=compute_fee(q.amount_minor, observed))


def _max_price_within(limit_minor: int, observed: dict[str, ObservedValue]) -> int:
    """Largest price whose price + fee stays within limit_minor."""
    bps = observed[FEE_SOURCE_ID].value
    price = limit_minor * 10_000 // (10_000 + bps)
    while price > 0 and price + apply_bps(price, bps) > limit_minor:
        price -= 1
    return price
