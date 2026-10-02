"""The only path from the agent to the outside world.

Every side effect goes through the gate, the decision and its templated reason are logged,
and the effect runs only on allow. PolicyState is updated here, after the effect succeeds.

A hire is a small transaction with a re-check between approval and payment:

  1. evaluate()                -> allow: the hire is approved but no money moves yet
  2. tell the freelancer we accept the price, read their reply
     (they might change the price; the user might revoke the policy meanwhile)
  3. recheck_before_release()  -> allow: only now does the escrow hold the money

For hires the executor, not the agent, supplies the evidence: the Profile comes from the
marketplace and the quote is the freelancer's latest inbound Message from our own inbox.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from app.contracts import Decision, HireProposal, Message, SignedMandate
from app.escrow import SimulatedEscrow
from app.ledger import ChainLog
from app.llm import LLMError
from app.marketplace import SimulatedMarketplace
from app.money import format_hkd
from app.policy import (HireRequest, PendingRelease, PolicyState, evaluate, evaluate_message, make_decision,
                        parse_quote, recheck_before_release, verify_mandate)

Hook = Callable[["GatedExecutor"], None]


class GatedExecutor:
    def __init__(self, signed: SignedMandate, trusted_user_pubkey: str, marketplace: SimulatedMarketplace,
                 escrow: SimulatedEscrow, log: ChainLog, now: Callable[[], datetime],
                 state: Optional[PolicyState] = None):
        self.signed = signed
        self.mandate = signed.mandate
        self.policy = signed.mandate.policy
        self.user_pubkey = trusted_user_pubkey
        self.marketplace = marketplace
        self.escrow = escrow
        self.log = log
        self.now = now
        self.state = state or PolicyState()
        self.inbox: dict[str, list[Message]] = {}       # freelancer_id -> inbound messages
        self.hooks: dict[str, list[Hook]] = {}          # e.g. "hire_approved" -> callbacks (tests, demos)
        self._msg_count = 0

    # --- helpers -----------------------------------------------------------------

    def _log(self, action: Optional[dict], decision: Decision) -> Decision:
        # The log fills rule_id, decision and the templated reason from the Decision itself.
        self.log.append("gate_decision", {"action": action, "details": decision.details}, decision)
        return decision

    def _message(self, freelancer_id: str, direction: str, text: str) -> Message:
        self._msg_count += 1
        return Message(message_id=f"msg-{self._msg_count}", platform=self.marketplace.name,
                       freelancer_id=freelancer_id, direction=direction, text=text,
                       untrusted=(direction == "inbound"), sent_at=self.now())

    def _fire(self, event: str) -> None:
        for hook in self.hooks.get(event, []):
            hook(self)

    # --- user controls -----------------------------------------------------------

    def check_mandate(self) -> Decision:
        """Verify the user's signature once, before the agent does anything."""
        return self._log({"kind": "verify_mandate", "policy_id": self.policy.policy_id},
                         verify_mandate(self.signed, self.user_pubkey))

    def revoke(self) -> None:
        """The user revokes the policy. Every later gate check denies with R8."""
        self.state.revoked_at = self.now()
        self.log.append("policy_revoked", {"policy_id": self.policy.policy_id,
                                              "revoked_at": self.state.revoked_at.isoformat()})

    # --- actions -----------------------------------------------------------------

    def send_message(self, freelancer_id: str, text: str) -> Optional[Message]:
        """Returns the freelancer's reply, or None if the gate refused or the reply failed."""
        msg = self._message(freelancer_id, "outbound", text)
        decision = self._log({"kind": "send_message", "message": msg.model_dump(mode="json")},
                             evaluate_message(self.policy, self.state, msg, self.now()))
        if decision.outcome != "allow":
            return None
        self.state.messages_sent[freelancer_id] = self.state.messages_sent.get(freelancer_id, 0) + 1
        try:
            reply_text = self.marketplace.deliver(freelancer_id, text)
        except (LLMError, KeyError) as exc:
            self.log.append("message_failed", {"freelancer_id": freelancer_id, "error": type(exc).__name__})
            return None
        reply = self._message(freelancer_id, "inbound", reply_text)
        self.inbox.setdefault(freelancer_id, []).append(reply)
        self.log.append("message_received", {"message": reply.model_dump(mode="json"), "label": "SIMULATED"})
        return reply

    def hire(self, proposal: HireProposal) -> Decision:
        """Approve, confirm, re-check, then fund. Returns the decision that ended the attempt."""
        try:
            profile = self.marketplace.profile(proposal.freelancer_id)
            quote = self.inbox[proposal.freelancer_id][-1]
        except (KeyError, IndexError):
            return self._log({"kind": "hire", "proposal": proposal.model_dump(mode="json")},
                             make_decision("R0", error="no profile or quote on record"))

        # 1. Time of check.
        request = HireRequest(proposal=proposal, profile=profile, quote=quote)
        decision = self._log({"kind": "hire", **request.model_dump(mode="json")},
                             evaluate(self.policy, self.state, request, self.now()))
        if decision.outcome != "allow":
            return decision
        total = decision.details["total"]
        if not isinstance(total, int):
            raise RuntimeError("gate allowed a hire without a total")
        self.state.pending = PendingRelease(proposal=proposal, approved_total_minor=total,
                                            current_price_minor=proposal.price.amount_minor, approved_at=self.now())
        self.log.append("hire_approved", {"proposal_id": proposal.proposal_id, "total_minor": total})
        self._fire("hire_approved")

        # 2. Confirm with the freelancer. If they restate a different (or unclear) price, the re-check catches it.
        self._confirm(proposal)

        # 3. Time of use.
        release = self._log({"kind": "release", "proposal_id": proposal.proposal_id},
                            recheck_before_release(self.policy, self.state, self.now()))
        if release.outcome != "allow":
            self.state.pending = None
            self.log.append("hire_cancelled", {"proposal_id": proposal.proposal_id, "rule_id": release.rule_id})
            return release

        order, entries = self.escrow.open_order(proposal, total, self.policy.budget_cap.currency, self.now())
        self.state.committed_minor += total
        self.state.order_times.append(self.now())
        self.state.pending = None
        self.log.append("order_created", {
            "order": order.model_dump(mode="json"),
            "ledger_entries": [e.model_dump(mode="json") for e in entries],
            "committed_minor": self.state.committed_minor,
        })
        return release

    def _confirm(self, proposal: HireProposal) -> None:
        pending = self.state.pending
        if pending is None:
            return
        text = f"Great, we'd like to go ahead at {format_hkd(proposal.price.amount_minor)} through the platform. Please confirm."
        reply = self.send_message(proposal.freelancer_id, text)
        if reply is None:
            # Could not confirm the price with the freelancer: treat the price as unknown (fail closed).
            self.state.pending = pending.model_copy(update={"current_price_minor": None})
            return
        q = parse_quote(reply.text)
        self.log.append("quote_parsed", {"freelancer_id": proposal.freelancer_id, "status": q.status,
                                            "amount_minor": q.amount_minor, "found": q.found, "stage": "confirmation"})
        if q.status == "ok":
            self.state.pending = pending.model_copy(update={"current_price_minor": q.amount_minor})
        elif q.status == "ambiguous":
            self.state.pending = pending.model_copy(update={"current_price_minor": None})
        # "none" (e.g. "Confirmed, starting now") leaves the approved price in place.
