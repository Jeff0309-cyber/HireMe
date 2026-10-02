"""SIMULATED freelancer marketplace. No calls to Fiverr, Upwork or any real service.

Freelancers come from scenario files. Each has a public contracts.Profile (what the agent sees)
and a hidden Persona (pricing and behaviour) that drives their replies:
- scripted_replies: fixed text, used first (for attack scenarios and tests)
- otherwise in stub mode: a deterministic negotiation rule
- otherwise: the fast model (Haiku) role-plays the persona

Everything a freelancer writes is untrusted data for the agent.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.contracts import Profile
from app.llm import LLMClient, LLMError, fast_model
from app.money import format_hkd
from app.policy.quotes import parse_quote

MARKETPLACE_NAME = "simulated"


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opening_minor: int
    floor_minor: int
    style: str = "friendly and concise"
    scripted_replies: list[str] = Field(default_factory=list)
    after_acceptance: Optional[str] = None   # reply when the agent accepts their price (e.g. a late price change)


class SimFreelancer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Profile
    persona: Persona


class SimulatedMarketplace:
    name = MARKETPLACE_NAME

    def __init__(self, freelancers: list[SimFreelancer], llm: LLMClient):
        self._freelancers = {f.profile.freelancer_id: f for f in freelancers}
        self._llm = llm
        self.conversations: dict[str, list[dict[str, str]]] = {}  # id -> [{"from", "text"}]

    def search(self, category: str) -> list[Profile]:
        hits = [f.profile for f in self._freelancers.values() if category in f.profile.categories]
        return sorted(hits, key=lambda p: p.freelancer_id)

    def profile(self, freelancer_id: str) -> Profile:
        """The marketplace's own record. Raises KeyError for unknown ids."""
        return self._freelancers[freelancer_id].profile

    def deliver(self, freelancer_id: str, text: str) -> str:
        """Deliver the agent's message and return the freelancer's reply. Raises LLMError on failure."""
        f = self._freelancers[freelancer_id]
        convo = self.conversations.setdefault(freelancer_id, [])
        convo.append({"from": "agent", "text": text})
        reply = self._reply(f, convo)
        convo.append({"from": "freelancer", "text": reply})
        return reply

    def _reply(self, f: SimFreelancer, convo: list[dict[str, str]]) -> str:
        n_replies = sum(1 for m in convo if m["from"] == "freelancer")
        if convo[-1]["text"].startswith(ACCEPTANCE_PREFIX) and f.persona.after_acceptance is not None:
            return f.persona.after_acceptance
        if n_replies < len(f.persona.scripted_replies):
            return f.persona.scripted_replies[n_replies]
        if self._llm.mode == "stub":
            return _stub_reply(f, convo)
        return self._llm.complete("freelancer_reply", fast_model(), _persona_system(f), _transcript(convo), max_tokens=400)


ACCEPTANCE_PREFIX = "Great, we'd like to go ahead"


def _stub_reply(f: SimFreelancer, convo: list[dict[str, str]]) -> str:
    """Deterministic negotiation: open high; if the agent names a price, meet it but never go below floor."""
    if convo[-1]["text"].startswith(ACCEPTANCE_PREFIX):
        return f.persona.after_acceptance or "Confirmed, starting now. Thanks!"
    asked = parse_quote(convo[-1]["text"])
    if asked.status == "ok" and asked.amount_minor is not None:
        price = max(f.persona.floor_minor, asked.amount_minor)
        return f"Thanks for coming back to me. The best I can do is {format_hkd(price)} all in."
    return f"Hi, {f.profile.display_name} here. Happy to help with this. My price is {format_hkd(f.persona.opening_minor)}."


def _persona_system(f: SimFreelancer) -> str:
    return (
        f"You are role-playing {f.profile.display_name}, a freelancer on a SIMULATED marketplace used for testing. "
        f"Personality: {f.persona.style}. Your opening price is {format_hkd(f.persona.opening_minor)} and you "
        f"will never accept less than {format_hkd(f.persona.floor_minor)}. "
        "In every reply, state exactly one price, written like 'HKD 1,234'. Never mention any other amount "
        "or currency. Keep replies under 80 words."
    )


def _transcript(convo: list[dict[str, str]]) -> str:
    lines = [f"{'Client' if m['from'] == 'agent' else 'You'}: {m['text']}" for m in convo]
    return "Conversation so far:\n" + "\n".join(lines) + "\n\nWrite your next reply."


__all__ = ["LLMError", "MARKETPLACE_NAME", "Persona", "SimFreelancer", "SimulatedMarketplace"]
