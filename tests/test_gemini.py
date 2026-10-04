"""Gemini: the REST client, the clarifying chat and the hiring agent, against a scripted fake API."""
import json

import pytest

from app.agent.clarify import Clarifier
from app.agent.hiring import HiringSession, RegisterForm
from app.ledger.keys import demo_key
from app.llm import LLMClient, LLMError
from app.llm import gemini
from app.marketplace.catalog import catalog
from test_hiring import LOGO, clock_from

HONEST = next(g.gig_id for g in catalog() if g.category == "logo_design" and g.persona == "honest"
              and g.seller.rating_x100 >= 450 and g.seller.completed_orders >= 10 and g.seller.verified)


def reply(*parts, finish="STOP"):
    return {"candidates": [{"content": {"role": "model", "parts": list(parts)}, "finishReason": finish}]}


def call(name, **args):
    return {"functionCall": {"name": name, "args": args, "id": f"c_{name}"}, "thoughtSignature": "c2ln"}


class FakeGemini:
    """Plays back scripted responses and records every request (url, body)."""
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, url, headers, body):
        assert headers["x-goog-api-key"] == "test-key" and "key=" not in url
        self.requests.append((url, json.loads(json.dumps(body))))
        r = self.responses.pop(0)
        return r if isinstance(r, tuple) else (200, r)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GIGGUARD_GEMINI_MODEL", raising=False)


def client(fake, tmp_path, mode="live"):
    return LLMClient(mode, cache_dir=tmp_path, provider="gemini", http_post=fake)


# --- REST client ------------------------------------------------------------------------------

def test_overloaded_model_falls_back(tmp_path):
    fake = FakeGemini([(503, {"error": {"message": "high demand"}}), (429, {"error": {"message": "busy"}}), reply({"text": "ok"})])
    out = client(fake, tmp_path).gemini_generate("t", "gemini-3.8-flash", {"contents": []})
    assert gemini.answer_text(out) == "ok"
    urls = [u for u, _ in fake.requests]
    assert "gemini-3.8-flash" in urls[0] and "gemini-3.5-flash" in urls[1] and gemini.FALLBACK_MODEL in urls[2]


def test_all_models_overloaded(tmp_path):
    fake = FakeGemini([(503, {"error": {"message": "x"}})] * 3)
    with pytest.raises(LLMError, match="overloaded"):
        client(fake, tmp_path).gemini_generate("t", "gemini-3.8-flash", {"contents": []})


@pytest.mark.parametrize("resp", [(400, {"error": {"message": "bad"}}), (200, {"candidates": []}),
                                  (200, reply({"text": "x"}, finish="SAFETY"))])
def test_errors_become_llm_errors(tmp_path, resp):
    with pytest.raises(LLMError):
        client(FakeGemini([resp]), tmp_path).gemini_generate("t", "gemini-3.8-flash", {"contents": []})


def test_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        client(FakeGemini([]), tmp_path).gemini_generate("t", "m", {"contents": []})


def test_cache_and_offline_replay(tmp_path):
    fake = FakeGemini([reply({"text": "hello"})])
    client(fake, tmp_path).gemini_generate("t", "m", {"contents": [1]})
    assert gemini.answer_text(client(FakeGemini([]), tmp_path, "offline").gemini_generate("t", "m", {"contents": [1]})) == "hello"


# --- clarifying chat ------------------------------------------------------------------------------

def ask_turn(thought="Thinking about logos"):
    return reply({"text": thought, "thought": True},
                 {"text": json.dumps({"status": "ask", "message": "A couple of questions:",
                                      "questions": [{"question": "What style?", "options": ["Minimal", "Playful"]}]})})


def ready_turn(budget=2000):
    return reply({"text": json.dumps({"status": "ready", "message": "Great, drafted.", "brief": {
        "title": "Bubble tea logo", "category": "logo_design", "keywords": ["logo"], "budget_hkd": budget,
        "deadline_days": 7, "min_words": None, "max_words": None, "must_haves": ["bilingual"]}})})


def test_gemini_asks_then_drafts(tmp_path):
    c = Clarifier(LOGO, client(FakeGemini([ask_turn(), ready_turn()]), tmp_path))
    t = c.start()
    assert not c.ready and t.questions[0].options == ["Minimal", "Playful"] and t.thinking == "Thinking about logos"
    c.answer("Minimal, please")
    assert c.ready and c.brief.title == "Bubble tea logo" and c.brief.budget_minor == 200_000
    assert c.brief.source == "gemini" and "Minimal, please" in c.brief.vision


def test_gemini_cannot_invent_the_budget(tmp_path):
    c = Clarifier(LOGO, client(FakeGemini([ready_turn(budget=9000)]), tmp_path))
    c.start()
    assert c.brief.budget_minor == 200_000          # the user's HKD 2,000, not the model's 9,000
    assert any("isn't written in your text" in w for w in c.brief.warnings)


def test_gemini_failure_falls_back_to_rule_questions(tmp_path):
    c = Clarifier("Edit some reels", client(FakeGemini([(400, {"error": {"message": "nope"}})]), tmp_path))
    t = c.start()
    assert "couldn't answer" in t.text and t.source == "rules" and t.questions


def test_round_limit_forces_a_draft(tmp_path):
    fake = FakeGemini([ask_turn(), ask_turn(), ask_turn(), ready_turn()])
    c = Clarifier(LOGO, client(fake, tmp_path))
    c.start()
    for i in range(3):
        c.answer(f"answer {i}")
    assert c.ready
    assert "finish now" in fake.requests[-1][1]["systemInstruction"]["parts"][0]["text"]


# --- hiring agent ------------------------------------------------------------------------------------

def test_gemini_agent_proposes_through_the_toolbox(tmp_path):
    fake = FakeGemini([
        reply({"text": "Weighing the options", "thought": True}, {"text": "Searching."}, call("search_gigs")),
        reply(call("message_seller", gig_id=HONEST, package="Basic", message="Could you confirm the total price in HKD?")),
        reply(call("propose_hire", gig_id=HONEST, package="Basic", why="Best fit and confirmed the price.")),
    ])
    s = HiringSession(clock_from(), demo_key("user"), demo_key("agent"), client(fake, tmp_path))
    s.stage = "drafted"
    s.register(RegisterForm(title="Bubble tea logo", category="logo_design", budget_hkd="2000", deadline_days=7))
    s.search()
    assert s.stage == "proposed" and s.proposal.gig_id == HONEST and s.escrow.ledger == []
    kinds = [a["kind"] for a in s.activity]
    assert {"thinking", "agent", "tool", "result"} <= set(kinds)
    # The model's content (with its thought signature) goes back unchanged, then our function response.
    second = fake.requests[1][1]["contents"]
    assert second[1]["parts"][-1]["thoughtSignature"] == "c2ln"
    fr = second[2]["parts"][0]["functionResponse"]
    assert fr["name"] == "search_gigs" and fr["id"] == "c_search_gigs" and "untrusted_listing" in json.dumps(fr)
    assert s.snapshot()["ai"]["agent"] == "gemini"
