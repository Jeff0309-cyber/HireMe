"""The one-page demo: /mandate, /run, /log, /tamper and the static page."""
import copy
import re

import pytest
from fastapi.testclient import TestClient

from app.api import web
from app.api.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web, "SESSION", web._Session())      # fresh demo session per test
    return TestClient(app)


def default_mandate(client):
    return client.get("/mandate").json()["mandate"]


# --- page ---------------------------------------------------------------------------------------------

def test_page_is_one_static_html_file_with_no_build_step(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert 'name="viewport"' in html                                     # mobile-friendly
    assert not re.search(r"<script[^>]+src=", html)                      # no external JS / bundle
    assert not re.search(r"<link[^>]+stylesheet", html)
    for needed in ("Sign mandate", "Run mission", "Tamper with a copy", "Pinned head hash", "SIMULATED"):
        assert needed in html


def test_page_never_renders_data_as_html(client):
    # Freelancer text is untrusted: the page must build DOM with text nodes only.
    html = client.get("/").text
    assert not re.search(r"\.(?:inner|outer)HTML\s*=|insertAdjacentHTML|document\.write|eval\(", html)


# --- /mandate -----------------------------------------------------------------------------------------

def test_sign_mandate(client):
    out = client.post("/mandate", json={"mandate": default_mandate(client)}).json()
    assert out["signed_mandate"]["mandate"]["category"] == "copywriting"
    assert len(out["signed_mandate"]["signature"]) == 128 and len(out["policy_hash"]) == 64
    assert client.get("/mandate").json()["signed_mandate"] == out["signed_mandate"]


@pytest.mark.parametrize("path, value", [
    (("policy", "budget_cap", "amount_minor"), 2000.5),           # float money
    (("policy", "budget_cap", "amount_minor"), "200000"),         # string money
    (("category",), "crypto_trading"),                            # not in allowed_categories
    (("policy", "expires_at"), "2026-01-01T00:00:00Z"),           # before issued_at
    (("policy", "approved_by_admin"), True),                      # unknown field
])
def test_bad_mandates_are_rejected(client, path, value):
    m = copy.deepcopy(default_mandate(client))
    target = m
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert client.post("/mandate", json={"mandate": m}).status_code == 422


# --- /run and /log ---------------------------------------------------------------------------------------

def test_log_before_run_is_404(client):
    assert client.get("/log").status_code == 404
    assert client.post("/tamper", json={"kind": "edit"}).status_code == 409


def test_run_mission_and_read_log(client):
    out = client.post("/run", json={}).json()
    s = out["summary"]
    assert (s["hired"], s["order_state"], s["addon_rule"], s["flagged_messages"]) == ("f_hana", "RELEASED", "R1", 3)
    assert s["completed_as_scripted"] and out["verify"]["ok"]
    assert out["head_hash"] == out["entries"][-1]["entry_hash"]
    assert client.get("/log").json() == out


def test_signed_mandate_changes_the_run(client):
    m = default_mandate(client)
    m["policy"]["budget_cap"]["amount_minor"] = 100_000                   # HKD 1,000: the HKD 1,575 hire can't fit
    m["policy"]["auto_approve_limit"]["amount_minor"] = 100_000
    client.post("/mandate", json={"mandate": m})
    out = client.post("/run", json={}).json()
    assert out["summary"]["hired"] == "f_hana" and out["summary"]["order_state"] is None
    assert [e["rule_id"] for e in out["entries"] if e["rule_id"] == "R1"] == ["R1"]


def test_mandate_edited_after_signing_is_refused(client):
    signed = client.post("/mandate", json={"mandate": default_mandate(client)}).json()["signed_mandate"]
    signed["mandate"]["policy"]["budget_cap"]["amount_minor"] = 9_999_999   # edit in the browser, keep signature
    out = client.post("/run", json={"signed_mandate": signed}).json()
    rules = [e["rule_id"] for e in out["entries"] if e["rule_id"]]
    assert rules == ["R17"] and out["summary"]["hired"] is None


def test_no_eligible_freelancer(client):
    m = default_mandate(client)
    m["policy"]["eligibility"]["min_rating_x100"] = 500
    client.post("/mandate", json={"mandate": m})
    out = client.post("/run", json={}).json()
    assert out["summary"]["hired"] is None
    assert any(e["event_type"] == "no_eligible_freelancer" for e in out["entries"])


# --- /tamper ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["edit", "delete", "reorder", "swap_signature", "truncate"])
def test_tamper_fails_verification_and_leaves_original_intact(client, kind):
    original = client.post("/run", json={}).json()
    out = client.post("/tamper", json={"kind": kind}).json()
    assert out["original"]["ok"] and out["original"]["message"].startswith(f"{len(original['entries'])} entries")
    assert not out["tampered"]["ok"]
    assert out["pinned_head"] == original["head_hash"]
    assert client.get("/log").json() == original                            # the real log is untouched
    if kind == "truncate":
        assert out["without_pinned_head"]["ok"]                              # invisible without the pin
    else:
        assert out["tampered"]["broken_seq"] == out["tampered_seq"]


def test_tamper_rejects_unknown_kind(client):
    client.post("/run", json={})
    assert client.post("/tamper", json={"kind": "rm -rf"}).status_code == 422
