"""SIMULATED escrow. No real money moves.

After the gate allows a hire, open_order() turns the HireProposal into an Order and records
the money movements as LedgerEntries: one "hold" for the price and one "fee" per fee line.
Fees are computed from data/observed.json only (hard rule 4); if the fee entry is missing,
compute_fee raises and the hire never reaches the gate.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.contracts import Fee, HireProposal, LedgerEntry, Milestone, Money, Order
from app.money import apply_bps, require_minor
from app.observed import ObservedValue

FEE_SOURCE_ID = "sim_marketplace_service_fee_bps"


class FeeQuote(BaseModel):
    fee_minor: int
    source_id: str
    simulated: bool

    def as_fee(self, currency: str = "HKD") -> Fee:
        return Fee(name="service_fee", amount=Money(amount_minor=self.fee_minor, currency=currency),
                   source_id=self.source_id)


def compute_fee(amount_minor: int, observed: dict[str, ObservedValue], source_id: str = FEE_SOURCE_ID) -> FeeQuote:
    entry = observed[source_id]                       # KeyError if missing -> caller fails closed
    if entry.unit != "basis_points":
        raise ValueError(f"{source_id} has unit {entry.unit}, expected basis_points")
    return FeeQuote(fee_minor=apply_bps(amount_minor, entry.value), source_id=source_id, simulated=entry.is_simulated)


class SimulatedEscrow:
    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.entries: list[LedgerEntry] = []

    def open_order(self, proposal: HireProposal, total_minor: int, currency: str, now: datetime) -> tuple[Order, list[LedgerEntry]]:
        """Create the order and hold its money. total_minor is the gate-approved total (fees, FX included)."""
        require_minor(total_minor)
        order_id = f"order-{len(self.orders) + 1}"
        order = Order(
            order_id=order_id, proposal_id=proposal.proposal_id, platform=proposal.platform,
            freelancer_id=proposal.freelancer_id, total=Money(amount_minor=total_minor, currency=currency),
            milestones=[m.model_copy(update={"status": "funded"}) for m in proposal.milestones],
            status="created", created_at=now,
        )
        new = [self._entry(order_id, "hold", proposal.price, now)]
        new += [self._entry(order_id, "fee", f.amount, now) for f in proposal.fees]
        self.orders[order_id] = order
        self.entries += new
        return order, new

    def release_milestone(self, order_id: str, milestone_id: str, now: datetime) -> LedgerEntry:
        """Pay out one funded milestone. Not used by the agent yet; here so the shape is exercised."""
        order = self.orders[order_id]
        ms = next(m for m in order.milestones if m.milestone_id == milestone_id)
        if ms.status != "funded":
            raise ValueError(f"{milestone_id} is {ms.status}, cannot release")
        milestones: list[Milestone] = [m.model_copy(update={"status": "released"}) if m is ms else m
                                       for m in order.milestones]
        self.orders[order_id] = order.model_copy(update={"milestones": milestones})
        entry = self._entry(order_id, "release", ms.amount, now, milestone_id)
        self.entries.append(entry)
        return entry

    def held_minor(self) -> int:
        """Money currently held: holds + fees minus releases."""
        held = sum(e.amount.amount_minor for e in self.entries if e.kind in ("hold", "fee"))
        return held - sum(e.amount.amount_minor for e in self.entries if e.kind in ("release", "refund"))

    def _entry(self, order_id: str, kind: str, amount: Money, now: datetime, milestone_id: str | None = None) -> LedgerEntry:
        return LedgerEntry(entry_id=f"le-{len(self.entries) + 1}", order_id=order_id, milestone_id=milestone_id,
                           kind=kind, amount=amount, at=now)
