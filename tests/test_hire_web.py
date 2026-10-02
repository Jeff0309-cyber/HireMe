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


def register_logo(client):
    b = client.post("/hire/vision", json={"text": LOGO}).json()["brief"]
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
    assert s["stage"] == "drafted" and s["brief"]["budget_minor"] == 200_000
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
    s = client.post("/hire/vision", json={"text": "Translate my menu into Japanese, HKD 800, 5 days"}).json()
    assert s["stage"] == "drafted" and s["mandate"] is None and s["brief"]["category"] == "translation"
    assert client.post("/hire/reset").json()["stage"] == "new"
