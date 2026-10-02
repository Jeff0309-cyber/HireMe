"""Table-driven boundary tests for every gate rule.

Each table row is (description, overrides, expected rule_id). Rows come in pairs where possible:
the last value that passes, then the first value that fails.
"""
import string
from datetime import datetime, timedelta, timezone

import pytest

from app.contracts import Fee, FxQuote, Milestone, Money, Policy
from app.ledger.keys import demo_key
from app.policy import (RULES, PendingRelease, PolicyState, escalation_expired, evaluate, evaluate_message,
                        make_decision, proposal_total_minor, recheck_before_release, render_reason, sign_mandate,
                        verify_mandate)
from conftest import (NOW, USER_PUB, hkd, make_hire, make_message, make_policy, make_profile, make_proposal,
                      make_quote, make_signed, two_milestones)

UTC = timezone.utc
ONE_US = timedelta(microseconds=1)


def hire(price=100_000, fee=5_000, quote=None, proposal_kw=None, profile_kw=None):
    """A hire request whose quote matches its price unless told otherwise."""
    return make_hire(proposal=make_proposal(price=price, fee=fee, **(proposal_kw or {})),
                     profile=make_profile(**(profile_kw or {})),
                     quote=make_quote(quote if quote is not None else f"My price is HKD {price // 100}.{price % 100:02d}"))


_DEFAULT = object()


def decide(req=_DEFAULT, policy=None, state=None, now=NOW):
    return evaluate(policy or make_policy(), state or PolicyState(), hire() if req is _DEFAULT else req, now)


def test_baseline_is_allowed():
    d = decide()
    assert (d.outcome, d.rule_id) == ("allow", "OK_HIRE")
    assert d.details == {"freelancer": "f_ok", "total": 105_000, "committed": 0, "cap": 250_000}


# --- R1 budget cap --------------------------------------------------------------------------

R1_CASES = [
    # cap 80,000; committed + price + fee
    ("exactly the cap", 0, 76_000, 4_000, "OK_HIRE"),
    ("one cent over", 0, 76_001, 4_000, "R1"),
    ("committed + total exactly the cap", 30_000, 46_000, 4_000, "OK_HIRE"),
    ("committed + total one cent over", 30_001, 46_000, 4_000, "R1"),
    ("fees push it over", 0, 80_000, 1, "R1"),
    ("already at the cap", 80_000, 100, 0, "R1"),
]


@pytest.mark.parametrize("label, committed, price, fee, rule", R1_CASES, ids=[c[0] for c in R1_CASES])
def test_r1_budget_cap(label, committed, price, fee, rule):
    policy = make_policy(budget_cap=hkd(80_000), auto_approve_limit=None)
    d = decide(hire(price, fee), policy=policy, state=PolicyState(committed_minor=committed))
    assert d.rule_id == rule


def test_r1_counts_fx_rounded_up():
    # USD 100.00 at 7.800001 = HKD 780.0001 -> rounds up to 78,001 cents. Cap 78,000 must deny.
    fx = FxQuote(from_currency="USD", to_currency="HKD", rate_micros=7_800_001, source_id="x", observed_at=NOW)
    p = make_proposal(fees=[], fx=fx).model_copy(update={"price": Money(amount_minor=10_000, currency="USD")})
    assert proposal_total_minor(p) == 78_001
    # (A USD price can't pass R13, which only reads HKD quotes, so check R1's arithmetic directly.)
    policy = make_policy(budget_cap=hkd(78_000), auto_approve_limit=None)
    state = PolicyState(pending=PendingRelease(proposal=p, approved_total_minor=78_001,
                                               current_price_minor=10_000, approved_at=NOW))
    assert recheck_before_release(policy, state, NOW).rule_id == "R1"


def test_r1_reason_format():
    d = decide(hire(76_001, 16_000), policy=make_policy(budget_cap=hkd(80_000), auto_approve_limit=None))
    assert render_reason(d) == "Rule R1 | cap=80000 | total=92001 | committed=0 | DENY"


# --- R2 platform, R3 category -------------------------------------------------------------------

@pytest.mark.parametrize("platform, rule", [("simulated", "OK_HIRE"), ("sim_upwork", "R2")])
def test_r2_platform(platform, rule):
    req = hire(proposal_kw={"platform": platform}, profile_kw={"platform": platform})
    req = req.model_copy(update={"quote": req.quote.model_copy(update={"platform": platform})})
    assert decide(req).rule_id == rule
    assert evaluate_message(make_policy(), PolicyState(), make_message(platform=platform), NOW).rule_id == \
        ("OK_MESSAGE" if rule == "OK_HIRE" else "R2")


@pytest.mark.parametrize("label, proposal_cat, offered, rule", [
    ("allowed and offered", "logo_design", ["logo_design"], "OK_HIRE"),
    ("not in allow-list", "video_editing", ["video_editing"], "R3"),
    ("allowed but freelancer doesn't offer it", "logo_design", ["video_editing"], "R3"),
])
def test_r3_category(label, proposal_cat, offered, rule):
    assert decide(hire(proposal_kw={"category": proposal_cat}, profile_kw={"categories": offered})).rule_id == rule


# --- R4 eligibility --------------------------------------------------------------------------

R4_CASES = [
    ("rating at minimum", {"rating_x100": 450}, "OK_HIRE", None),
    ("rating one below", {"rating_x100": 449}, "R4", "min_rating"),
    ("orders at minimum", {"completed_orders": 10}, "OK_HIRE", None),
    ("orders one below", {"completed_orders": 9}, "R4", "min_completed_orders"),
    ("verified", {"verified": True}, "OK_HIRE", None),
    ("not verified", {"verified": False}, "R4", "verified"),
    ("blocked", {"freelancer_id": "f_bad"}, "R4", "blocked"),
]


@pytest.mark.parametrize("label, profile_kw, rule, check", R4_CASES, ids=[c[0] for c in R4_CASES])
def test_r4_eligibility(label, profile_kw, rule, check):
    fid = profile_kw.get("freelancer_id", "f_ok")
    req = hire(proposal_kw={"freelancer_id": fid}, profile_kw=profile_kw)
    req = req.model_copy(update={"quote": req.quote.model_copy(update={"freelancer_id": fid})})
    d = decide(req)
    assert d.rule_id == rule
    if check:
        assert d.details["check"] == check


def test_r4_unverified_ok_when_not_required():
    policy = make_policy(eligibility={"min_rating_x100": 450, "min_completed_orders": 10, "verified_required": False})
    assert decide(hire(profile_kw={"verified": False}), policy=policy).rule_id == "OK_HIRE"


def test_r4_blocked_freelancer_cannot_be_messaged():
    d = evaluate_message(make_policy(), PolicyState(), make_message(freelancer_id="f_bad"), NOW)
    assert (d.rule_id, d.details["check"]) == ("R4", "blocked")


# --- R5 milestone-only ----------------------------------------------------------------------------

R5_CASES = [
    ("milestone_only, 2 that add up", True, two_milestones(100_000), "OK_HIRE"),
    ("milestone_only, 3 that add up", True, two_milestones(50_000) + two_milestones(50_000), "OK_HIRE"),
    ("milestone_only, only 1", True, [Milestone(milestone_id="m", title="all", amount=hkd(100_000))], "R5"),
    ("milestone_only, none", True, [], "R5"),
    ("milestones one cent short", False, two_milestones(99_999), "R5"),
    ("no milestones, not required", False, [], "OK_HIRE"),
]


@pytest.mark.parametrize("label, milestone_only, milestones, rule", R5_CASES, ids=[c[0] for c in R5_CASES])
def test_r5_milestones(label, milestone_only, milestones, rule):
    # unique ids for the 3-milestone row
    milestones = [m.model_copy(update={"milestone_id": f"m{i}"}) for i, m in enumerate(milestones)]
    d = decide(hire(proposal_kw={"milestones": milestones}), policy=make_policy(milestone_only=milestone_only))
    assert d.rule_id == rule


# --- R6 off-platform -------------------------------------------------------------------------------

@pytest.mark.parametrize("forbid, channel, rule", [
    (True, "platform_escrow", "OK_HIRE"), (True, "platform_direct", "OK_HIRE"),
    (True, "off_platform", "R6"), (False, "off_platform", "OK_HIRE"),
])
def test_r6_offplatform(forbid, channel, rule):
    d = decide(hire(proposal_kw={"payment_channel": channel}), policy=make_policy(forbid_offplatform=forbid))
    assert d.rule_id == rule


# --- R7 expiry ----------------------------------------------------------------------------------------

ISSUED = datetime(2026, 9, 30, tzinfo=UTC)
EXPIRES = datetime(2026, 12, 31, tzinfo=UTC)


@pytest.mark.parametrize("label, now, rule", [
    ("one microsecond before issued_at", ISSUED - ONE_US, "R7"),
    ("exactly issued_at", ISSUED, "OK_HIRE"),
    ("one microsecond before expiry", EXPIRES - ONE_US, "OK_HIRE"),
    ("exactly expires_at", EXPIRES, "R7"),
    ("after expiry", EXPIRES + timedelta(days=1), "R7"),
])
def test_r7_expiry(label, now, rule):
    assert decide(now=now).rule_id == rule
    expected_msg = "OK_MESSAGE" if rule == "OK_HIRE" else "R7"
    assert evaluate_message(make_policy(), PolicyState(), make_message(), now).rule_id == expected_msg


# --- R8 revocation --------------------------------------------------------------------------------------

@pytest.mark.parametrize("label, state, rule", [
    ("not revoked", PolicyState(), "OK_HIRE"),
    ("revoked one microsecond in the future", PolicyState(revoked_at=NOW + ONE_US), "OK_HIRE"),
    ("revoked exactly now", PolicyState(revoked_at=NOW), "R8"),
    ("revoked earlier", PolicyState(revoked_at=NOW - timedelta(hours=1)), "R8"),
    ("same version still current", PolicyState(latest_policy_version=1), "OK_HIRE"),
    ("superseded by version 2", PolicyState(latest_policy_version=2), "R8"),
])
def test_r8_revocation(label, state, rule):
    assert decide(state=state).rule_id == rule


# --- R9 velocity ---------------------------------------------------------------------------------------

def _orders(*minutes_ago):
    return PolicyState(order_times=[NOW - timedelta(minutes=m) for m in minutes_ago])


@pytest.mark.parametrize("label, state, rule", [
    ("N-1 orders in window", _orders(1), "OK_HIRE"),
    ("N orders in window", _orders(1, 59), "R9"),
    ("one order exactly at the window edge (excluded)", _orders(1, 60), "OK_HIRE"),
    ("old orders don't count", _orders(61, 120, 500), "OK_HIRE"),
])
def test_r9_velocity(label, state, rule):
    policy = make_policy(velocity={"max_orders": 2, "window_minutes": 60}, max_hires=10)
    assert decide(policy=policy, state=state).rule_id == rule


# --- supporting rules ---------------------------------------------------------------------------------

@pytest.mark.parametrize("label, req, rule", [
    ("quote has a different price", hire(quote="HKD 999"), "R13"),
    ("quote has two prices", hire(quote="HKD 1,000 or HKD 900"), "R13"),
    ("quote has no price", hire(quote="Price on request"), "R13"),
    ("profile for another freelancer", hire(profile_kw={"freelancer_id": "f_x"}), "R14"),
    ("zero price", hire(price=0, fee=0), "R14"),
    ("fee in another currency", hire(proposal_kw={"fees": [Fee(name="f", amount=Money(amount_minor=1, currency="USD"), source_id="x")]}), "R15"),
])
def test_supporting_rules(label, req, rule):
    assert decide(req).rule_id == rule


@pytest.mark.parametrize("label, total, rule", [
    ("at the auto-approve limit", 150_000, "OK_HIRE"),
    ("one cent over", 150_001, "R12"),
])
def test_r12_auto_approve(label, total, rule):
    d = decide(hire(price=total - 5_000))
    assert (d.rule_id, d.outcome) == (rule, "allow" if rule == "OK_HIRE" else "escalate")


def test_r11_hire_limit():
    assert decide(state=_orders(500)).rule_id == "R11"


def test_escalation_expiry():
    d = decide(hire(price=150_000))
    assert d.details["escalation_expires_at"] == "2026-10-01T10:00:00Z"
    policy = make_policy()
    assert not escalation_expired(NOW, NOW + timedelta(seconds=3599), policy)
    assert escalation_expired(NOW, NOW + timedelta(seconds=3600), policy)


@pytest.mark.parametrize("label, msg, rule", [
    ("fine", make_message(), "OK_MESSAGE"),
    ("100 chars", make_message(text="x" * 100), "OK_MESSAGE"),
    ("101 chars", make_message(text="x" * 101), "R16"),
    ("inbound", make_message(direction="inbound", untrusted=True), "R16"),
])
def test_message_rules(label, msg, rule):
    assert evaluate_message(make_policy(), PolicyState(), msg, NOW).rule_id == rule


def test_message_limit():
    state = PolicyState(messages_sent={"f_ok": 2})
    assert evaluate_message(make_policy(), state, make_message(), NOW).rule_id == "R16"


# --- recheck_before_release (time-of-check vs time-of-use) -------------------------------------------------

def _pending(current_price=100_000, total=105_000, committed=0, **state_kw):
    pending = PendingRelease(proposal=make_proposal(), approved_total_minor=total,
                             current_price_minor=current_price, approved_at=NOW)
    return PolicyState(pending=pending, committed_minor=committed, **state_kw)


@pytest.mark.parametrize("label, state, now, rule", [
    ("nothing changed", _pending(), NOW, "OK_RELEASE"),
    ("nothing pending", PolicyState(), NOW, "R0"),
    ("price went up one cent", _pending(current_price=100_001), NOW, "R10"),
    ("price went down", _pending(current_price=90_000), NOW, "R10"),
    ("price became unclear", _pending(current_price=None), NOW, "R10"),
    ("revoked after approval", _pending(revoked_at=NOW - ONE_US), NOW, "R8"),
    ("superseded after approval", _pending(latest_policy_version=2), NOW, "R8"),
    ("expired after approval", _pending(), EXPIRES, "R7"),
    ("another order used the budget", _pending(committed=145_001), NOW, "R1"),
    ("budget exactly used up", _pending(committed=145_000), NOW, "OK_RELEASE"),
])
def test_recheck_before_release(label, state, now, rule):
    assert recheck_before_release(make_policy(), state, now).rule_id == rule


# --- mandate signature --------------------------------------------------------------------------------------

def test_verify_mandate():
    signed = make_signed()
    assert verify_mandate(signed, USER_PUB).rule_id == "OK_MANDATE"
    policy = Policy.model_validate({**signed.mandate.policy.model_dump(), "budget_cap": hkd(9_999_999)})
    tampered = signed.model_copy(update={"mandate": signed.mandate.model_copy(update={"policy": policy})})
    assert verify_mandate(tampered, USER_PUB).rule_id == "R17"
    assert verify_mandate(sign_mandate(signed.mandate, demo_key("attacker")), USER_PUB).rule_id == "R17"
    assert verify_mandate(signed, "not-hex").rule_id == "R17"


# --- fail closed --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    None, "hire everyone", 42, {}, {"proposal": {"price": 1000.0}},
    {**make_hire().model_dump(), "approved_by_admin": True},
])
def test_malformed_proposals_denied(bad):
    d = decide(bad)
    assert (d.outcome, d.rule_id) == ("deny", "R0")


def test_naive_now_fails_closed():
    d = decide(now=datetime(2026, 10, 1))
    assert (d.outcome, d.rule_id) == ("deny", "R0")
    assert recheck_before_release(make_policy(), _pending(), datetime(2026, 10, 1)).rule_id == "R0"


def test_dict_proposal_accepted_when_valid():
    assert decide(make_hire().model_dump(mode="json")).rule_id == "OK_HIRE"


def test_gate_is_deterministic_and_does_not_mutate_state():
    state = _orders(5)
    before = state.model_copy(deep=True)
    assert decide(state=state) == decide(state=state)
    assert state == before


# --- templates ------------------------------------------------------------------------------------------------

def test_every_rule_renders():
    for rule_id, rule in RULES.items():
        assert rule.outcome in ("allow", "deny", "escalate")
        reason = render_reason(make_decision(rule_id, **{k: 1 for k in rule.keys}))
        assert reason.startswith(f"Rule {rule_id} | ") and reason.endswith(rule.outcome.upper())


def test_make_decision_rejects_wrong_keys():
    with pytest.raises(ValueError):
        make_decision("R1", cap=1, total=2)                 # missing committed
    with pytest.raises(ValueError):
        make_decision("R1", cap=1, total=2, committed=0, note="LLM text")


def test_nine_policy_rules_exist():
    assert {f"R{i}" for i in range(1, 10)} <= set(RULES)
