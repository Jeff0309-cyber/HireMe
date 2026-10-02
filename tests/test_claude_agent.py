"""The Claude hiring agent, driven by a scripted fake of the Messages API (no network)."""
import json
from types import SimpleNamespace

import pytest

from app.agent.hiring import HiringSession, RegisterForm
from app.ledger.keys import demo_key
from app.llm import LLMClient
from app.marketplace.catalog import catalog
from test_hiring import LOGO, clock_from

HONEST = next(g.gig_id for g in catalog() if g.category == "logo_design" and g.persona == "honest"
              and g.seller.rating_x100 >= 450 and g.seller.completed_orders >= 10 and g.seller.verified)
INJECTION = next(g.gig_id for g in catalog() if g.category == "logo_design" and g.persona == "injection")


def tool(name, **inp):
    return {"type": "tool_use", "id": f"tu_{name}_{len(json.dumps(inp))}", "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


class FakeMessages:
    """Returns one scripted assistant turn per call and records the requests."""
    def __init__(self, turns):
        self.turns, self.requests = list(turns), []

    def create(self, **params):
        self.requests.append(json.loads(json.dumps(params)))
        if not self.turns:
            raise AssertionError("agent made more calls than scripted")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        content = [{"type": "thinking", "thinking": "", "signature": "sig"}] + turn
        stop = "tool_use" if any(b["type"] == "tool_use" for b in turn) else "end_turn"
        data = {"id": "msg", "type": "message", "role": "assistant", "content": content, "stop_reason": stop}
        return SimpleNamespace(model_dump=lambda **kw: data)


def agent_session(turns, tmp_path, mode="live"):
    fake = FakeMessages(turns)
    llm = LLMClient(mode, cache_dir=tmp_path, sdk_client=SimpleNamespace(messages=fake))
    s = HiringSession(clock_from(), demo_key("user"), demo_key("agent"), llm)
    s.brief = None
    s.stage = "drafted"
    s.register(RegisterForm(title="Bubble tea shop logo, Chinese and English", category="logo_design",
                            budget_hkd="2000", deadline_days=7))
    return s, fake


HAPPY = [
    [text("Looking at the logo gigs."), tool("search_gigs")],
    [text("Asking the best fit to confirm."), tool("message_seller", gig_id=HONEST, package="Basic",
                                                     message="Hi! Could you do the Basic package within 7 days? Total price in HKD please.")],
    [text("They confirmed the listed price."), tool("propose_hire", gig_id=HONEST, package="Basic",
                                                     why="Strong rating and they confirmed the listed price.")],
]


def test_agent_proposes_after_confirmed_quote(tmp_path):
    s, fake = agent_session(HAPPY, tmp_path)
    s.search()
    assert s.stage == "proposed" and s.proposal.gig_id == HONEST
    assert s.proposal.why.startswith("Strong rating") and s.proposal.precheck["outcome"] == "allow"
    assert s.escrow.ledger == []                                         # proposing never pays
    kinds = [a["kind"] for a in s.activity]
    assert "agent" in kinds and "tool" in kinds and "result" in kinds
    assert any(e["event_type"] == "agent_note" for e in s.log.entries())

    first = fake.requests[0]
    assert first["model"] == "claude-opus-5-5" and "temperature" not in first
    assert "HKD 2,000.00" in first["system"] and {t["name"] for t in first["tools"]} == {
        "search_gigs", "message_seller", "propose_hire", "finish_without_hire"}
    # Thinking blocks are passed back unchanged, and seller text is fenced as untrusted.
    second = fake.requests[1]
    assert second["messages"][1]["content"][0]["type"] == "thinking"
    third = json.dumps(fake.requests[2]["messages"][-1])
    assert "untrusted_seller_message" in third


def test_offline_mode_replays_the_recorded_run(tmp_path):
    s, _ = agent_session(HAPPY, tmp_path)
    s.search()
    replay, fake = agent_session([], tmp_path, mode="offline")          # no scripted turns: must come from cache
    replay.search()
    assert replay.stage == "proposed" and replay.proposal.gig_id == HONEST and fake.requests == []


def test_fooled_model_cannot_propose_a_flagged_seller(tmp_path):
    turns = [
        [tool("search_gigs")],
        [tool("message_seller", gig_id=INJECTION, package="Basic", message="Could you confirm the price in HKD?")],
        [text("They say it's pre-approved, proposing."), tool("propose_hire", gig_id=INJECTION, package="Basic", why="pre-approved")],
        [tool("finish_without_hire", why="Nobody suitable.")],
    ]
    s, fake = agent_session(turns, tmp_path)
    s.search()
    assert s.stage == "no_match" and s.proposal is None
    refused = [a for a in s.activity if a["kind"] == "result" and a["name"] == "propose_hire"]
    assert refused and not refused[0]["ok"] and "refused" in refused[0]["summary"]


@pytest.mark.parametrize("bad_message", ["Call me on +852 9123 4567", "We accept your price!", "Pay via PayPal please"])
def test_agent_messages_go_through_the_outbound_guard(tmp_path, bad_message):
    turns = [[tool("search_gigs")],
             [tool("message_seller", gig_id=HONEST, package="Basic", message=bad_message)],
             [tool("finish_without_hire", why="stop")]]
    s, _ = agent_session(turns, tmp_path)
    s.search()
    assert not [m for m in s.conversation if m["from"] == "agent"]
    assert any(e["event_type"] == "message_blocked" for e in s.log.entries())


def test_cannot_propose_without_a_confirmed_quote(tmp_path):
    turns = [[tool("search_gigs")], [tool("propose_hire", gig_id=HONEST, package="Basic", why="looks good")],
             [tool("finish_without_hire", why="stop")]]
    s, _ = agent_session(turns, tmp_path)
    s.search()
    assert s.proposal is None


def test_seller_limit(tmp_path):
    others = [g.gig_id for g in catalog() if g.category == "logo_design"][:4]
    turns = [[tool("search_gigs")]] + [
        [tool("message_seller", gig_id=g, package="Basic", message="Total price in HKD?")] for g in others] + [
        [tool("finish_without_hire", why="stop")]]
    s, _ = agent_session(turns, tmp_path)
    s.search()
    messaged = {m["gig_id"] for m in s.conversation if m["from"] == "agent"}
    assert len(messaged) <= 3


def test_api_failure_falls_back_to_rules(tmp_path):
    import anthropic

    class Boom(anthropic.APIError):
        def __init__(self):          # skip the SDK's request/response plumbing
            pass
    s, _ = agent_session([Boom()], tmp_path)
    s.search()
    assert any(a["kind"] == "error" for a in s.activity)
    assert any("rule-based search" in a.get("text", "") for a in s.activity)
    assert s.stage == "proposed" and s.proposal is not None        # the rules still found someone


def test_model_stops_without_tools(tmp_path):
    s, _ = agent_session([[text("I'm not sure what to do.")]], tmp_path)
    s.search()
    assert s.stage == "no_match" and s.no_match_reason


def test_stub_mode_uses_rules_not_claude():
    s = HiringSession(clock_from(), demo_key("user"), demo_key("agent"), LLMClient("stub"))
    b = s.draft(LOGO)
    s.register(RegisterForm(title=b.title, category=b.category, budget_hkd="2000", deadline_days=7))
    assert not s.uses_agent
    s.search()
    assert s.snapshot()["ai"]["agent"] == "rules"
