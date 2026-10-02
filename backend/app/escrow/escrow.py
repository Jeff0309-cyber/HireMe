"""SIMULATED escrow state machine. No real money moves.

    CREATED --fund--> FUNDED --deliver--> DELIVERED --release--> RELEASED
                         \\                    \\
                          +-----hold-----------+--> HELD   (failed check, gate re-check denied, dispute)

- fund()    calls the gate (evaluate) first. Only allow moves money into escrow.
- deliver() checks the deliverable: file type and word count, nothing else.
- release() calls recheck_before_release() first. A deny puts the order on HELD, so money that
            was approved earlier still cannot move if the policy was revoked, expired, or the
            price changed in between.
- Every operation takes an idempotency key. Repeating a call with the same key returns the
  first result and moves no money twice. Reusing a key for a different operation is an error.
- Every transition and gate decision is written to the signed chain log.

Simplification: one funded-but-unreleased order at a time (PolicyState has one `pending` slot).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Callable, Literal, Optional

from app.contracts import Decision, LedgerEntry, Money, Policy
from app.ledger import ChainLog
from app.policy import (HireRequest, PendingRelease, PolicyState, evaluate, make_decision, proposal_total_minor,
                        recheck_before_release)

State = Literal["CREATED", "FUNDED", "DELIVERED", "RELEASED", "HELD"]
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"FUNDED"},
    "FUNDED": {"DELIVERED", "HELD"},
    "DELIVERED": {"RELEASED", "HELD"},
    "RELEASED": set(),
    "HELD": set(),
}


class EscrowError(Exception):
    """Illegal transition, unknown order, or idempotency-key misuse."""


@dataclass(frozen=True)
class DeliverableSpec:
    allowed_types: tuple[str, ...] = (".md", ".txt")
    min_words: int = 150
    max_words: int = 400


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    file_type: str
    words: int
    problem: Optional[str] = None


def check_deliverable(filename: str, content: str, spec: DeliverableSpec) -> CheckResult:
    """File type + word count only. Deliberately dumb: anything subtler needs a human."""
    suffix = PurePosixPath(filename).suffix.lower()
    words = len(content.split())
    if suffix not in spec.allowed_types:
        return CheckResult(False, suffix, words, f"file type {suffix or '(none)'} not in {list(spec.allowed_types)}")
    if not spec.min_words <= words <= spec.max_words:
        return CheckResult(False, suffix, words, f"{words} words, expected {spec.min_words}-{spec.max_words}")
    return CheckResult(True, suffix, words)


@dataclass
class EscrowOrder:
    order_id: str
    request: HireRequest
    state: State = "CREATED"
    total_minor: int = 0                 # gate-approved total, set when funded
    history: list[str] = field(default_factory=lambda: ["CREATED"])


class Escrow:
    label = "SIMULATED"

    def __init__(self, policy: Policy, state: PolicyState, log: ChainLog, now: Callable[[], datetime],
                 spec: DeliverableSpec = DeliverableSpec()):
        self.policy = policy
        self.state = state
        self.log = log
        self.now = now
        self.spec = spec
        self.orders: dict[str, EscrowOrder] = {}
        self.ledger: list[LedgerEntry] = []
        self._idem: dict[str, tuple[str, Any]] = {}      # key -> (fingerprint of the call, result)

    # --- idempotency -----------------------------------------------------------------------

    def _once(self, key: str, op: str, args: dict, fn: Callable[[], Any]) -> Any:
        fingerprint = hashlib.sha256(json.dumps([op, args], sort_keys=True).encode()).hexdigest()
        if key in self._idem:
            seen, result = self._idem[key]
            if seen != fingerprint:
                raise EscrowError(f"idempotency key {key!r} was already used for a different call")
            self.log.append("escrow_idempotent_replay", {"idempotency_key": key, "op": op})
            return result
        result = fn()
        self._idem[key] = (fingerprint, result)
        return result

    # --- helpers --------------------------------------------------------------------------------

    def _order(self, order_id: str) -> EscrowOrder:
        if order_id not in self.orders:
            raise EscrowError(f"unknown order {order_id}")
        return self.orders[order_id]

    def _move(self, order: EscrowOrder, to: str, why: dict) -> None:
        if to not in ALLOWED_TRANSITIONS[order.state]:
            raise EscrowError(f"{order.order_id}: cannot go from {order.state} to {to}")
        self.log.append("escrow_transition", {"order_id": order.order_id, "from": order.state, "to": to,
                                              "label": self.label, **why})
        order.state = to  # type: ignore[assignment]
        order.history.append(to)

    def _book(self, order: EscrowOrder, kind: str, amount: Money) -> LedgerEntry:
        entry = LedgerEntry(entry_id=f"le-{len(self.ledger) + 1}", order_id=order.order_id, kind=kind,
                            amount=amount, at=self.now())
        self.ledger.append(entry)
        return entry

    def _gate_log(self, check: str, order: EscrowOrder, decision: Decision) -> Decision:
        self.log.append("gate_decision", {"action": {"kind": check, "order_id": order.order_id,
                                                     "proposal": order.request.proposal.model_dump(mode="json")},
                                          "details": decision.details}, decision)
        return decision

    # --- operations -------------------------------------------------------------------------------

    def create(self, order_id: str, request: HireRequest, idempotency_key: str) -> EscrowOrder:
        def do() -> EscrowOrder:
            if order_id in self.orders:
                raise EscrowError(f"order {order_id} already exists")
            order = EscrowOrder(order_id=order_id, request=request)
            self.orders[order_id] = order
            self.log.append("escrow_created", {"order_id": order_id, "proposal_id": request.proposal.proposal_id,
                                               "freelancer_id": request.proposal.freelancer_id, "label": self.label})
            return order
        return self._once(idempotency_key, "create", {"order_id": order_id,
                                                      "proposal": request.proposal.proposal_id}, do)

    def fund(self, order_id: str, idempotency_key: str) -> Decision:
        """Gate first. On allow: hold price + fees, and record the order as pending release."""
        def do() -> Decision:
            order = self._order(order_id)
            if order.state != "CREATED":
                raise EscrowError(f"{order_id}: can only fund a CREATED order, it is {order.state}")
            decision = self._gate_log("fund", order, evaluate(self.policy, self.state, order.request, self.now()))
            if decision.outcome != "allow":
                return decision
            if self.state.pending is not None:
                # Fail closed: this simple escrow handles one unreleased order at a time.
                return self._gate_log("fund", order, make_decision("R0", error="another order is awaiting release"))
            p = order.request.proposal
            order.total_minor = proposal_total_minor(p)
            self._book(order, "hold", p.price)
            for fee in p.fees:
                self._book(order, "fee", fee.amount)
            self.state.committed_minor += order.total_minor
            self.state.order_times.append(self.now())
            self.state.pending = PendingRelease(proposal=p, approved_total_minor=order.total_minor,
                                                current_price_minor=p.price.amount_minor, approved_at=self.now())
            self._move(order, "FUNDED", {"total_minor": order.total_minor})
            return decision
        return self._once(idempotency_key, "fund", {"order_id": order_id}, do)

    def deliver(self, order_id: str, filename: str, content: str, idempotency_key: str) -> CheckResult:
        def do() -> CheckResult:
            order = self._order(order_id)
            if order.state != "FUNDED":
                raise EscrowError(f"{order_id}: can only deliver a FUNDED order, it is {order.state}")
            check = check_deliverable(filename, content, self.spec)
            self.log.append("deliverable_checked", {"order_id": order_id, "filename": filename,
                                                    "file_type": check.file_type, "words": check.words,
                                                    "ok": check.ok, "problem": check.problem})
            self._move(order, "DELIVERED" if check.ok else "HELD", {"filename": filename})
            return check
        digest = hashlib.sha256(content.encode()).hexdigest()
        return self._once(idempotency_key, "deliver", {"order_id": order_id, "filename": filename,
                                                       "sha256": digest}, do)

    def release(self, order_id: str, idempotency_key: str) -> Decision:
        """recheck_before_release first. On deny the order goes to HELD and no money moves."""
        def do() -> Decision:
            order = self._order(order_id)
            if order.state != "DELIVERED":
                raise EscrowError(f"{order_id}: can only release a DELIVERED order, it is {order.state}")
            # recheck adds the pending total itself, so pass committed spend *excluding* this order.
            view = self.state.model_copy(update={"committed_minor": self.state.committed_minor - order.total_minor})
            decision = self._gate_log("release", order, recheck_before_release(self.policy, view, self.now()))
            if decision.outcome != "allow":
                self._move(order, "HELD", {"rule_id": decision.rule_id})
                return decision
            self._book(order, "release", order.request.proposal.price)
            self.state.pending = None
            self._move(order, "RELEASED", {"amount_minor": order.request.proposal.price.amount_minor})
            return decision
        return self._once(idempotency_key, "release", {"order_id": order_id}, do)

    def hold(self, order_id: str, reason_code: str, idempotency_key: str) -> EscrowOrder:
        def do() -> EscrowOrder:
            order = self._order(order_id)
            self._move(order, "HELD", {"code": reason_code})
            return order
        return self._once(idempotency_key, "hold", {"order_id": order_id, "code": reason_code}, do)

    # --- simple ledger views -----------------------------------------------------------------------

    def held_minor(self) -> int:
        """Price money still in escrow (holds minus releases and refunds). Fees are the platform's."""
        held = sum(e.amount.amount_minor for e in self.ledger if e.kind == "hold")
        return held - sum(e.amount.amount_minor for e in self.ledger if e.kind in ("release", "refund"))

    def released_minor(self) -> int:
        return sum(e.amount.amount_minor for e in self.ledger if e.kind == "release")

    def fees_minor(self) -> int:
        return sum(e.amount.amount_minor for e in self.ledger if e.kind == "fee")
