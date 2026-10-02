"""The main app: describe a job, register it, let the agent find a freelancer, confirm, pay.

  GET  /                 the page (static/hire.html, vanilla JS)
  GET  /hire/categories  categories the simulated marketplace offers
  POST /hire/vision      {text} -> draft brief (nothing registered yet)
  POST /hire/register    reviewed brief -> signed mandate; the decision log starts
  POST /hire/search      agent searches GigMarket (SIMULATED), messages sellers, proposes a hire.
                         With an API key this is Claude (runs in the background; poll /hire/state).
  POST /hire/confirm     USER confirms -> gate -> money held in SIMULATED escrow
  POST /hire/deliver     SIMULATED delivery -> file type + word count check
  POST /hire/release     USER approves -> gate re-check -> money released
  GET  /hire/state       everything the page needs to redraw
  GET  /hire/log         the signed log, pinned head hash, verification
  POST /hire/tamper      tamper with a COPY of the log and show verification failing
  POST /hire/reset       start over

One shared demo session (a lock keeps requests from interleaving). DEMO keys, SIMULATED money.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agent.hiring import FlowError, HiringSession, RegisterForm
from app.agent.stub import stub_llm
from app.ledger.keys import demo_key
from app.llm import LLMClient, load_env_file, mode_from_env
from app.marketplace.catalog import CATEGORIES
from verify_log import verify_lines

load_env_file()   # ANTHROPIC_API_KEY and GIGGUARD_LLM_MODE from .env, before the session is created
STATIC = Path(__file__).resolve().parent / "static"
router = APIRouter()


def _real_clock() -> datetime:
    return datetime.now(timezone.utc)


def new_session(clock=_real_clock) -> HiringSession:
    return HiringSession(clock, demo_key("user"), demo_key("agent"), LLMClient(mode_from_env(), stub=stub_llm))


class _Holder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.session = new_session()


HOLDER = _Holder()


class VisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)


class TamperIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["edit", "delete", "reorder", "swap_signature", "truncate"] = "edit"


def _step(fn):
    """Run one step under the lock and turn flow errors into HTTP errors."""
    with HOLDER.lock:
        try:
            fn(HOLDER.session)
        except FlowError as exc:
            raise HTTPException(409, str(exc)) from exc
        return HOLDER.session.snapshot()


@router.get("/", include_in_schema=False)
def page():
    return FileResponse(STATIC / "hire.html", media_type="text/html")


@router.get("/hire/categories")
def categories():
    return [{"id": c.id, "label": c.label, "deliverable": c.deliverable} for c in CATEGORIES.values()]


@router.post("/hire/vision")
def vision(body: VisionIn):
    def go(s: HiringSession):
        if s.stage not in ("new", "drafted"):
            HOLDER.session = s = new_session(s.clock)      # a new vision starts a new task
        s.draft(body.text)
    with HOLDER.lock:
        try:
            go(HOLDER.session)
        except FlowError as exc:
            raise HTTPException(422, str(exc)) from exc
        return HOLDER.session.snapshot()


@router.post("/hire/register")
def register(form: RegisterForm):
    return _step(lambda s: s.register(form))


@router.post("/hire/search")
def search():
    """Rule-based search runs inline. The Claude agent can take a minute, so it runs in a
    background thread and the page polls /hire/state to show what it is doing."""
    with HOLDER.lock:
        s = HOLDER.session
        try:
            s.begin_search()
        except FlowError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not s.uses_agent:
            s.search()
        else:
            threading.Thread(target=_search_in_background, args=(s,), daemon=True).start()
        return s.snapshot()


def _search_in_background(s: HiringSession) -> None:
    try:
        s.search()
    except Exception:  # noqa: BLE001 - search() already set stage=no_match and logged it
        pass


@router.post("/hire/confirm")
def confirm():
    return _step(lambda s: s.confirm())


@router.post("/hire/deliver")
def deliver():
    return _step(lambda s: s.deliver())


@router.post("/hire/release")
def release():
    return _step(lambda s: s.release())


@router.post("/hire/reset")
def reset():
    with HOLDER.lock:
        HOLDER.session = new_session(HOLDER.session.clock)
        return HOLDER.session.snapshot()


@router.get("/hire/state")
def state():
    with HOLDER.lock:
        return HOLDER.session.snapshot()


def _entries() -> list[dict]:
    s = HOLDER.session
    if s.log is None:
        raise HTTPException(404, "nothing registered yet")
    return s.log.entries()


@router.get("/hire/log")
def log():
    with HOLDER.lock:
        entries = _entries()
        s = HOLDER.session
        head = s.log.head_hash
        check = verify_lines([json.dumps(e, ensure_ascii=False) for e in entries], s.log.public_key_hex, expect_head=head)
        return {"entries": entries, "head_hash": head, "agent_pubkey": s.log.public_key_hex,
                "verify": {"ok": check.ok, "message": check.message}}


@router.post("/hire/tamper")
def tamper(body: Optional[TamperIn] = None):
    kind = body.kind if body else "edit"
    with HOLDER.lock:
        original = _entries()
        s = HOLDER.session
        pub, head = s.log.public_key_hex, s.log.head_hash
    if len(original) < 3:
        raise HTTPException(409, "the log is too short to tamper with yet")
    copy = json.loads(json.dumps(original))
    target = next((e["seq"] for e in copy if e["decision"] == "allow" and e["rule_id"] != "OK_MANDATE"), len(copy) // 2)
    target = min(target, len(copy) - 2)
    if kind == "edit":
        e = copy[target]
        e["decision"] = "deny" if e["decision"] == "allow" else "allow"
        what = f"changed the decision in entry {target} to {e['decision'].upper()}"
    elif kind == "delete":
        del copy[target]
        what = f"deleted entry {target}"
    elif kind == "reorder":
        copy[target], copy[target + 1] = copy[target + 1], copy[target]
        what = f"swapped entries {target} and {target + 1}"
    elif kind == "swap_signature":
        copy[target]["signature"] = original[target + 1]["signature"]
        what = f"put entry {target + 1}'s signature on entry {target}"
    else:
        copy = copy[:target]
        what = f"cut off every entry from {target} on"
    lines = lambda es: [json.dumps(x, ensure_ascii=False) for x in es]  # noqa: E731
    honest = verify_lines(lines(original), pub, expect_head=head)
    bad = verify_lines(lines(copy), pub, expect_head=head)
    unpinned = verify_lines(lines(copy), pub) if kind == "truncate" else None
    return {"kind": kind, "what": what, "tampered_seq": target, "pinned_head": head,
            "original": {"ok": honest.ok, "message": honest.message},
            "tampered": {"ok": bad.ok, "message": bad.message, "broken_seq": bad.broken_seq},
            "without_pinned_head": None if unpinned is None else {"ok": unpinned.ok, "message": unpinned.message}}
