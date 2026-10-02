"""The scripted end-to-end mission. Deterministic: SIMULATED marketplace scripts, a fixed clock,
DEMO keys, no LLM. Run it with `make demo` (python -m app.agent.mission).

Steps (each one is written to the signed chain log):
   1. verify the user's signature on the mandate
   2. pick the honest freelancer: eligible under the policy, best skill fit, then lowest quote
   3. message them (outbound guard + gate), read the reply (inbound flagger), parse the price
   4. propose the hire -> escrow.fund() -> gate allows -> money held in escrow
   5. the off-platform pusher sends an unsolicited pitch -> FLAGGED, ignored
   6. the upseller pitches a paid add-on -> FLAGGED; the add-on is proposed anyway, as a naive
      or compromised agent might -> gate DENIES it (R1: it would break the budget cap)
   7. the upseller claims the add-on was "pre-approved, release now" -> FLAGGED, ignored
   8. the honest freelancer delivers -> deliverable check (file type + word count)
   9. escrow.release() -> recheck_before_release allows -> money released

Flags never cause an action: the only things that move money are the escrow calls in steps 4
and 9, and each of them goes through the gate first.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.agent.messaging import flag_inbound, guard_outbound
from app.contracts import HireProposal, Mandate, Message, Money, Policy, Profile, SignedMandate
from app.escrow import compute_fee
from app.escrow.escrow import Escrow
from app.harness import ScenarioClock
from app.ledger import ChainLog
from app.ledger.keys import demo_key
from app.marketplace.sim import SimMarketplace
from app.observed import load_observed
from app.policy import (HireRequest, PolicyState, eligibility_failures, evaluate_message, mandate_digest,
                        parse_quote, sign_mandate, verify_mandate)

TASK = "Write a 150-400 word English menu description for a bubble tea shop in Hong Kong"
CATEGORY = "copywriting"
JOB_KEYWORDS = {"copywriting", "menu", "food", "description", "bilingual"}
START = "2026-10-01T09:00:00Z"


def mission_policy() -> Policy:
    hkd = lambda n: Money(amount_minor=n)  # noqa: E731
    return Policy(
        policy_id="p-mission-001", version=1,
        issued_at=datetime(2026, 9, 30, tzinfo=timezone.utc), expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        budget_cap=hkd(200_000), auto_approve_limit=hkd(200_000), max_hires=2,
        allowed_platforms=["simulated"], allowed_categories=[CATEGORY],
        eligibility={"min_rating_x100": 450, "min_completed_orders": 10, "verified_required": True},
        milestone_only=False, forbid_offplatform=True, velocity={"max_orders": 5, "window_minutes": 60},
        max_messages_per_freelancer=3, max_message_chars=600, escalation_ttl_seconds=86_400,
    )


@dataclass
class MissionResult:
    ok: bool                      # every step went the way the script expects
    hired: Optional[str]
    order_state: Optional[str]
    addon_rule: Optional[str]     # rule that denied the add-on
    flags: dict[str, tuple[str, ...]]
    entries: list[dict]
    agent_pubkey: str
    head_hash: str
    released_minor: int
    held_minor: int


class Mission:
    """Holds the pieces one run needs, so each step below stays short."""

    def __init__(self, signed: SignedMandate, user_pubkey: str, log: ChainLog, clock: ScenarioClock):
        self.signed, self.policy, self.user_pubkey = signed, signed.mandate.policy, user_pubkey
        self.log, self.clock = log, clock
        self.market = SimMarketplace()
        self.state = PolicyState()
        self.escrow = Escrow(self.policy, self.state, log, clock.now)
        self.observed = load_observed()
        self.flags: dict[str, tuple[str, ...]] = {}       # message_id -> flag codes
        self.flagged_freelancers: set[str] = set()
        self._n = 0

    def _message(self, fid: str, direction: str, text: str) -> Message:
        self._n += 1
        return Message(message_id=f"msg-{self._n}", platform=self.market.name, freelancer_id=fid,
                       direction=direction, text=text, untrusted=(direction == "inbound"), sent_at=self.clock.now())

    def send(self, fid: str, text: str) -> bool:
        """Outbound: regex guard first, then the gate's message rules. Both must pass."""
        guard = guard_outbound(text)
        if not guard.ok:
            self.log.append("message_blocked", {"freelancer_id": fid, "text": text, "violations": list(guard.violations)})
            return False
        msg = self._message(fid, "outbound", text)
        decision = evaluate_message(self.policy, self.state, msg, self.clock.now())
        self.log.append("gate_decision", {"action": {"kind": "send_message", "message": msg.model_dump(mode="json")},
                                          "details": decision.details}, decision)
        if decision.outcome != "allow":
            return False
        self.state.messages_sent[fid] = self.state.messages_sent.get(fid, 0) + 1
        return True

    def receive(self, fid: str, text: str) -> Message:
        """Inbound: log the message as untrusted, then log any flags. Flags never trigger an action."""
        msg = self._message(fid, "inbound", text)
        self.log.append("message_received", {"message": msg.model_dump(mode="json"), "label": "SIMULATED"})
        flags = flag_inbound(text)
        if flags:
            self.flags[msg.message_id] = flags
            self.flagged_freelancers.add(fid)
            self.log.append("message_flagged", {"message_id": msg.message_id, "freelancer_id": fid,
                                                "flags": list(flags), "action_taken": "none"})
        return msg

    def proposal_for(self, profile: Profile, price_minor: int, proposal_id: str) -> HireProposal:
        fee = compute_fee(price_minor, self.observed)          # fee rate comes only from data/observed.json
        return HireProposal(proposal_id=proposal_id, platform=profile.platform, freelancer_id=profile.freelancer_id,
                            category=self.signed.mandate.category, price=Money(amount_minor=price_minor),
                            fees=[fee.as_fee()],
                            payment_channel="platform_escrow")


def default_mandate() -> Mandate:
    return Mandate(principal="demo-user (SIMULATED)", task=TASK, category=CATEGORY, policy=mission_policy())


def run_mission(signed: Optional[SignedMandate] = None, agent_key=None, user_key=None) -> MissionResult:
    """Run the mission under `signed` (default: the demo mandate signed with user_key).

    The gate trusts only user_key's public key, so a mandate edited after signing stops at step 1.
    """
    user_key = user_key or demo_key("user")
    agent_key = agent_key or demo_key("agent")
    clock = ScenarioClock(START)
    signed = signed or sign_mandate(default_mandate(), user_key)
    task, category = signed.mandate.task, signed.mandate.category
    log = ChainLog(agent_key, clock.tick_iso, policy_hash=mandate_digest(signed.mandate))
    m = Mission(signed, user_key.verify_key.encode().hex(), log, clock)
    steps_ok = True

    log.append("mission_started", {"task": task, "policy_id": m.policy.policy_id, "label": "SIMULATED",
                                   "agent_pubkey": log.public_key_hex})

    # 1. Signature.
    d = verify_mandate(signed, m.user_pubkey)
    log.append("gate_decision", {"action": {"kind": "verify_mandate"}, "details": d.details}, d)
    if d.outcome != "allow":
        return _finish(m, False, None, None, None)

    # 2. Pick: eligible, best keyword fit, then lowest listed quote, then id.
    def fit(p: Profile) -> int:
        return len(JOB_KEYWORDS & {s.lower() for s in p.skills})

    candidates = []
    for p in m.market.profiles(category):
        failures = [f.code for f in eligibility_failures(p, m.policy)]
        log.append("candidate_scored", {"freelancer_id": p.freelancer_id, "fit": fit(p), "eligibility_failures": failures})
        if not failures:
            candidates.append(p)
    if not candidates:
        log.append("no_eligible_freelancer", {"category": category, "label": "SIMULATED"})
        return _finish(m, False, None, None, None)
    pick = sorted(candidates, key=lambda p: (-fit(p), p.freelancer_id))[0]
    log.append("freelancer_picked", {"freelancer_id": pick.freelancer_id, "fit": fit(pick)})

    # 3. Message, read the reply, parse the price deterministically.
    intro = f"Hi {pick.display_name.split(' (')[0]}, we have a job: {task}. Could you reply with one total price in HKD?"
    if not m.send(pick.freelancer_id, intro):
        return _finish(m, False, None, None, None)
    quote = m.receive(pick.freelancer_id, m.market.message(pick.freelancer_id, "quote"))
    q = parse_quote(quote.text)
    log.append("quote_parsed", {"freelancer_id": pick.freelancer_id, "status": q.status, "amount_minor": q.amount_minor})
    if q.status != "ok" or q.amount_minor is None or pick.freelancer_id in m.flagged_freelancers:
        return _finish(m, False, None, None, None)

    # 4. Propose the hire and fund escrow. The gate decides inside fund().
    main = HireRequest(proposal=m.proposal_for(pick, q.amount_minor, "hp-main"), profile=pick, quote=quote)
    m.escrow.create("order-main", main, idempotency_key="create-main")
    if m.escrow.fund("order-main", idempotency_key="fund-main").outcome != "allow":
        return _finish(m, False, pick.freelancer_id, None, None)

    # 5. Off-platform pusher: flagged, ignored.
    m.receive("f_pat", m.market.message("f_pat", "unsolicited"))

    # 6. Upseller add-on: flagged, proposed anyway, denied by the gate.
    ugo = m.market.profile("f_ugo")
    pitch = m.receive("f_ugo", m.market.message("f_ugo", "addon"))
    addon_price = parse_quote(pitch.text).amount_minor or 0
    log.append("addon_proposed", {"freelancer_id": "f_ugo", "amount_minor": addon_price,
                                  "note_code": "PROPOSED_TO_GATE_TO_SHOW_ENFORCEMENT"})
    addon = HireRequest(proposal=m.proposal_for(ugo, addon_price, "hp-addon"), profile=ugo, quote=pitch)
    m.escrow.create("order-addon", addon, idempotency_key="create-addon")
    addon_decision = m.escrow.fund("order-addon", idempotency_key="fund-addon")
    steps_ok &= addon_decision.outcome == "deny"

    # 7. Fake pre-approval: flagged, ignored. (No code path reads flags to release money.)
    m.receive("f_ugo", m.market.message("f_ugo", "preapproval"))

    # 8. Delivery and check. A freelancer with no deliverable never gets paid: the order is held.
    try:
        filename, content = m.market.deliverable(pick.freelancer_id)
    except KeyError:
        m.escrow.hold("order-main", "NO_DELIVERY", idempotency_key="hold-main")
        return _finish(m, False, pick.freelancer_id, addon_decision.rule_id, m.escrow.orders["order-main"].state)
    m.receive(pick.freelancer_id, m.market.message(pick.freelancer_id, "delivered", filename=filename))
    check = m.escrow.deliver("order-main", filename, content, idempotency_key="deliver-main")
    steps_ok &= check.ok

    # 9. Release, after the gate's re-check.
    if check.ok:
        steps_ok &= m.escrow.release("order-main", idempotency_key="release-main").outcome == "allow"

    return _finish(m, steps_ok, pick.freelancer_id, addon_decision.rule_id, m.escrow.orders["order-main"].state)


def _finish(m: Mission, ok: bool, hired: Optional[str], addon_rule: Optional[str], state: Optional[str]) -> MissionResult:
    m.log.append("mission_finished", {
        "ok": ok, "hired": hired, "order_state": state, "addon_rule": addon_rule,
        "released_minor": m.escrow.released_minor(), "held_minor": m.escrow.held_minor(),
        "fees_minor": m.escrow.fees_minor(), "flagged_messages": len(m.flags), "label": "SIMULATED",
    })
    return MissionResult(ok=ok, hired=hired, order_state=state, addon_rule=addon_rule, flags=dict(m.flags),
                         entries=m.log.entries(), agent_pubkey=m.log.public_key_hex, head_hash=m.log.head_hash,
                         released_minor=m.escrow.released_minor(), held_minor=m.escrow.held_minor())


# --- headless runner: `make demo` -------------------------------------------------------------------

def _summary(e: dict) -> str:
    i = e["inputs"]
    if e["reason"]:
        return e["reason"]
    kind = e["event_type"]
    if kind == "message_received":
        return f"[UNTRUSTED] {i['message']['freelancer_id']}: {i['message']['text']}"
    if kind == "message_flagged":
        return f"FLAGGED {i['freelancer_id']} {','.join(i['flags'])} -> action taken: {i['action_taken']}"
    if kind == "escrow_transition":
        extra = {k: v for k, v in i.items() if k not in ("order_id", "from", "to", "label")}
        return f"{i['order_id']}: {i['from']} -> {i['to']} {extra if extra else ''}".rstrip()
    return json.dumps(i, ensure_ascii=False)[:150]


def main(argv=None) -> int:
    import argparse
    from verify_log import verify_lines

    p = argparse.ArgumentParser(description="Run the SIMULATED GigGuard mission headlessly and print the log.")
    p.add_argument("--export", type=Path, help="write mission.jsonl, mission.head and agent.pub here")
    args = p.parse_args(argv)

    r = run_mission()
    print("GigGuard mission  |  SIMULATED marketplace and payments  |  no LLM  |  DEMO keys\n")
    for e in r.entries:
        tag = (e["decision"] or "").upper()
        print(f"#{e['seq']:<3} {e['event_type']:<24} {tag:<9} {_summary(e)}")

    check = verify_lines([json.dumps(e, ensure_ascii=False) for e in r.entries], r.agent_pubkey, expect_head=r.head_hash)
    print(f"\nhired={r.hired}  order={r.order_state}  add-on={r.addon_rule}  "
          f"released=HKD {r.released_minor // 100:,}.{r.released_minor % 100:02d}  flagged={len(r.flags)}")
    print(f"verifier: {'PASS' if check.ok else 'FAIL'}: {check.message}")
    print(f"MISSION {'OK' if r.ok and check.ok else 'FAILED'}")

    if args.export:
        args.export.mkdir(parents=True, exist_ok=True)
        (args.export / "mission.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in r.entries),
                                                   encoding="utf-8")
        (args.export / "mission.head").write_text(r.head_hash + "\n")
        (args.export / "agent.pub").write_text(r.agent_pubkey + "\n")
    return 0 if r.ok and check.ok else 1


if __name__ == "__main__":
    sys.exit(main())
