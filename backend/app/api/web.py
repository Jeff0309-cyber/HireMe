"""Endpoints for the one-page demo (static/index.html, served at /).

  GET  /          the page (vanilla JS, no build step)
  GET  /mandate   the default mandate, as an editable starting point
  POST /mandate   validate a mandate and sign it with the user key -> SignedMandate + policy_hash
  POST /run       run the SIMULATED mission under the last signed mandate (or one passed in)
  GET  /log       the last run's signed log, head hash and public key
  POST /tamper    tamper with a COPY of the last log and return the verifier's verdict

Keys: DEMO keys by default (reproducible). The gate trusts only the user key's public key, so
a signed mandate edited in the browser before /run is refused at the signature check (R17).
Everything here is SIMULATED; nothing is sent to a real marketplace or payment system.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from app.agent.mission import MissionResult, default_mandate, run_mission
from app.contracts import Mandate, SignedMandate
from app.ledger.keys import demo_key
from app.policy import mandate_digest, sign_mandate
from verify_log import verify_lines

STATIC = Path(__file__).resolve().parent / "static"
router = APIRouter()

USER_KEY = demo_key("user")
AGENT_KEY = demo_key("agent")


class _Session:
    """Last signed mandate and last run. One shared demo session; a lock keeps runs from interleaving."""
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.signed: Optional[SignedMandate] = None
        self.result: Optional[MissionResult] = None


SESSION = _Session()


class MandateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mandate: Mandate


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signed_mandate: Optional[SignedMandate] = None   # default: the last one signed via POST /mandate


class TamperIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["edit", "delete", "reorder", "swap_signature", "truncate"] = "edit"


def _signed_out(signed: SignedMandate) -> dict[str, Any]:
    return {"signed_mandate": signed.model_dump(mode="json"), "policy_hash": mandate_digest(signed.mandate),
            "trusted_user_pubkey": USER_KEY.verify_key.encode().hex(), "label": "SIMULATED"}


@router.get("/", include_in_schema=False)
def page():
    return FileResponse(STATIC / "index.html", media_type="text/html")


@router.get("/mandate")
def get_mandate():
    with SESSION.lock:
        if SESSION.signed:
            return _signed_out(SESSION.signed)
    return {"mandate": default_mandate().model_dump(mode="json"), "signed_mandate": None}


@router.post("/mandate")
def post_mandate(body: MandateIn):
    """Pydantic has already rejected malformed mandates (floats for money, bad category, ...) with 422."""
    signed = sign_mandate(body.mandate, USER_KEY)
    with SESSION.lock:
        SESSION.signed = signed
    return _signed_out(signed)


@router.post("/run")
def post_run(body: Optional[RunIn] = None):
    with SESSION.lock:
        signed = (body.signed_mandate if body else None) or SESSION.signed
        SESSION.result = run_mission(signed, agent_key=AGENT_KEY, user_key=USER_KEY)
        return _log_out(SESSION.result)


@router.get("/log")
def get_log():
    with SESSION.lock:
        if SESSION.result is None:
            raise HTTPException(404, "no mission has run yet; POST /run first")
        return _log_out(SESSION.result)


def _log_out(r: MissionResult) -> dict[str, Any]:
    check = verify_lines(_lines(r.entries), r.agent_pubkey, expect_head=r.head_hash)
    return {
        "summary": {"completed_as_scripted": r.ok, "hired": r.hired, "order_state": r.order_state,
                    "addon_rule": r.addon_rule, "released_minor": r.released_minor, "held_minor": r.held_minor,
                    "flagged_messages": len(r.flags)},
        "entries": r.entries, "head_hash": r.head_hash, "agent_pubkey": r.agent_pubkey,
        "verify": {"ok": check.ok, "message": check.message}, "label": "SIMULATED",
    }


def _lines(entries: list[dict]) -> list[str]:
    return [json.dumps(e, ensure_ascii=False) for e in entries]


@router.post("/tamper")
def post_tamper(body: Optional[TamperIn] = None):
    """Tamper with a deep COPY of the last log (the real one is untouched) and verify it."""
    kind = body.kind if body else "edit"
    with SESSION.lock:
        if SESSION.result is None:
            raise HTTPException(409, "no mission has run yet; POST /run first")
        r = SESSION.result
    original = r.entries
    copy = json.loads(json.dumps(original))
    # Target the first gate decision that moved money, or the middle of the log.
    target = next((e["seq"] for e in copy if e["rule_id"] in ("OK_HIRE", "R1")), len(copy) // 2)
    target = min(target, len(copy) - 2)

    if kind == "edit":
        e = copy[target]
        if e["decision"]:
            e["decision"] = "deny" if e["decision"] == "allow" else "allow"
            what = f"flipped the decision of entry {target} to {e['decision'].upper()}"
        else:
            e["event_type"] = e["event_type"] + "_edited"
            what = f"renamed the event in entry {target}"
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

    honest = verify_lines(_lines(original), r.agent_pubkey, expect_head=r.head_hash)
    tampered = verify_lines(_lines(copy), r.agent_pubkey, expect_head=r.head_hash)
    unpinned = verify_lines(_lines(copy), r.agent_pubkey) if kind == "truncate" else None
    return {
        "kind": kind, "what": what, "tampered_seq": target,
        "original": {"ok": honest.ok, "message": honest.message},
        "tampered": {"ok": tampered.ok, "message": tampered.message, "broken_seq": tampered.broken_seq},
        "without_pinned_head": None if unpinned is None else {"ok": unpinned.ok, "message": unpinned.message},
        "pinned_head": r.head_hash,
    }
