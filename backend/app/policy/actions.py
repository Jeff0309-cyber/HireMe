"""What the gate is asked to judge.

HireRequest is the `proposal` argument of evaluate(): the agent's HireProposal plus the
evidence the gate checks it against. The executor, not the agent, fills in `profile` (from the
marketplace's own record) and `quote` (the freelancer's latest inbound message from the
executor's inbox), so the agent cannot invent a rating or a price.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.contracts import HireProposal, Message, Profile


class HireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    proposal: HireProposal
    profile: Profile
    quote: Message
