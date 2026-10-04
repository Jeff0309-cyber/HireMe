"""The main app: page at / and the /hire/* endpoints."""
import re

import pytest
from fastapi.testclient import TestClient

from app.api import hire_web
from app.api.main import app
from test_hiring import LOGO, clock_from


@pytest.fixture
def client(monkeypatch):
    holder = hire_web._Holder()
    holder.session = hire_web.new_session(clock_from())
    monkeypatch.setattr(hire_web, "HOLDER", holder)
    return TestClient(app)


def chat_until_drafted(client, text=LOGO):
    """Paste the idea, answer the AI's questions until it drafts the task."""
    s = client.post("/hire/vision", json={"text": text}).json()
    for _ in range(4):
        if s["stage"] == "drafted":
            break
        s = client.post("/hire/answer", json={"text": "Chinese and English, vector source files"}).json()
    return s


def register_logo(client):
    b = chat_until_drafted(client)["brief"]
    return client.post("/hire/register", json={"title": b["title"], "category": b["category"], "budget_hkd": "2,000",
                                                "deadline_days": b["deadline_days"]})


def test_page(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    for needed in ("Hire a freelancer", "Describe the job", "Sign & register task", "Find me a freelancer",
                   "Confirm & pay", "Approve & release payment", "SIMULATED", 'name="viewport"', "Tamper with a copy"):
        assert needed in html
    assert not re.search(r"<script[^>]+src=|\.(?:inner|outer)HTML\s*=|insertAdjacentHTML|document\.write", html)


def test_categories(client):
    assert {c["id"] for c in client.get("/hire/categories").json()} >= {"logo_design", "copywriting"}


def test_full_flow_over_http(client):
    s = client.post("/hire/vision", json={"text": LOGO}).json()
    assert s["stage"] == "clarifying"
    ai = s["chat"][-1]
    assert ai["role"] == "ai" and ai["questions"] and ai["questions"][0]["options"]
    s = client.post("/hire/answer", json={"text": "Chinese and English, vector source files"}).json()
    assert s["stage"] == "drafted" and s["brief"]["budget_minor"] == 200_000
    assert [t["role"] for t in s["chat"]] == ["user", "ai", "user", "ai"]
    client.post("/hire/reset")
    s = register_logo(client).json()
    assert s["stage"] == "registered" and len(s["mandate"]["policy_hash"]) == 64
    s = client.post("/hire/search").json()
    assert s["stage"] == "proposed" and s["proposal"]["precheck"]["outcome"] == "allow"
    assert s["escrow"]["held_minor"] == 0
    assert all(a["label"] == "SIMULATED" for a in s["assessments"])
    s = client.post("/hire/confirm").json()
    assert s["stage"] == "funded" and s["order"]["state"] == "FUNDED"
    s = client.post("/hire/deliver").json()
    assert s["stage"] == "delivered"
    s = client.post("/hire/release").json()
    assert s["stage"] == "released" and s["escrow"]["released_minor"] == s["proposal"]["price_minor"]
    log = client.get("/hire/log").json()
    assert log["verify"]["ok"] and log["head_hash"] == log["entries"][-1]["entry_hash"]


def test_out_of_order_and_bad_input(client):
    assert client.post("/hire/confirm").status_code == 409
    assert client.get("/hire/log").status_code == 404
    assert client.post("/hire/vision", json={"text": ""}).status_code == 422
    client.post("/hire/vision", json={"text": LOGO})
    bad = client.post("/hire/register", json={"title": "Logo", "category": "logo_design", "budget_hkd": "2000.5.5",
                                              "deadline_days": 7})
    assert bad.status_code == 422


@pytest.mark.parametrize("kind", ["edit", "delete", "reorder", "swap_signature", "truncate"])
def test_tamper(client, kind):
    register_logo(client)
    client.post("/hire/search")
    out = client.post("/hire/tamper", json={"kind": kind}).json()
    assert out["original"]["ok"] and not out["tampered"]["ok"]


def test_new_vision_starts_a_new_task_and_reset(client):
    register_logo(client)
    s = chat_until_drafted(client, "Translate my menu into Japanese, HKD 800, 5 days")
    assert s["stage"] == "drafted" and s["mandate"] is None and s["brief"]["category"] == "translation"
    assert client.post("/hire/reset").json()["stage"] == "new"


def test_live_mode_without_a_key_uses_rules(monkeypatch):
    monkeypatch.setenv("GIGGUARD_LLM_MODE", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert hire_web.effective_mode() == "stub"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert hire_web.effective_mode() == "live"


def test_questions_ask_only_for_what_is_missing(client):
    s = client.post("/hire/vision", json={"text": "Edit 5 Instagram reels from my shop footage"}).json()
    asked = " ".join(q["question"] for q in s["chat"][-1]["questions"])
    assert "budget" in asked and "When do you need it" in asked
    s = client.post("/hire/answer", json={"text": "HKD 3,000, within 10 days"}).json()
    assert s["stage"] == "drafted"
    assert (s["brief"]["budget_minor"], s["brief"]["deadline_days"]) == (300_000, 10)   # from the user's answer


def test_skip_questions(client):
    client.post("/hire/vision", json={"text": LOGO})
    s = client.post("/hire/skip").json()
    assert s["stage"] == "drafted" and s["brief"]["category"] == "logo_design"
    assert client.post("/hire/skip").status_code == 409


def test_connections_are_reported(client):
    s = client.get("/hire/state").json()
    assert s["connections"][0]["name"].startswith("gigmarket")
