import os

# Tests never call a real model, even if a developer's .env says GIGGUARD_LLM_MODE=live.
os.environ["GIGGUARD_LLM_MODE"] = "stub"
os.environ["GIGGUARD_LLM_PROVIDER"] = "anthropic"
os.environ["GIGGUARD_MARKET"] = "local"        # MCP is tested separately (tests/test_mcp.py)

from datetime import datetime, timezone

import pytest

from app.contracts import Fee, HireProposal, Mandate, Message, Milestone, Money, Policy, Profile, SignedMandate
from app.ledger.keys import demo_key
from app.policy import HireRequest, PolicyState, sign_mandate

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
USER_KEY = demo_key("test-user")
USER_PUB = USER_KEY.verify_key.encode().hex()


def hkd(n: int) -> Money:
    return Money(amount_minor=n)


def make_policy(**overrides) -> Policy:
    fields = dict(
        policy_id="p-test", version=1,
        issued_at=datetime(2026, 9, 30, tzinfo=timezone.utc), expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        budget_cap=hkd(250_000), auto_approve_limit=hkd(150_000), max_hires=1,
        allowed_platforms=["simulated"], allowed_categories=["logo_design"], blocked_freelancers=["f_bad"],
        eligibility={"min_rating_x100": 450, "min_completed_orders": 10, "verified_required": True},
        milestone_only=False, forbid_offplatform=True, velocity={"max_orders": 10, "window_minutes": 60},
        max_messages_per_freelancer=2, max_message_chars=100, escalation_ttl_seconds=3600,
    )
    fields.update(overrides)
    return Policy(**fields)


def make_signed(**policy_overrides) -> SignedMandate:
    mandate = Mandate(principal="tester", task="Design a logo", category="logo_design",
                      policy=make_policy(**policy_overrides))
    return sign_mandate(mandate, USER_KEY)


def make_profile(**overrides) -> Profile:
    fields = dict(platform="simulated", freelancer_id="f_ok", display_name="OK", categories=["logo_design"],
                  skills=["logo"], rating_x100=480, completed_orders=50, verified=True)
    fields.update(overrides)
    return Profile(**fields)


def make_quote(text="My price is HKD 1,000.", **overrides) -> Message:
    fields = dict(message_id="in-1", platform="simulated", freelancer_id="f_ok", direction="inbound",
                  text=text, untrusted=True, sent_at=NOW)
    fields.update(overrides)
    return Message(**fields)


def make_proposal(price=100_000, fee=5_000, **overrides) -> HireProposal:
    fields = dict(proposal_id="hp-1", platform="simulated", freelancer_id="f_ok", category="logo_design",
                  price=hkd(price), fees=[Fee(name="service_fee", amount=hkd(fee), source_id="sim_marketplace_service_fee_bps")],
                  milestones=[], payment_channel="platform_escrow")
    fields.update(overrides)
    return HireProposal(**fields)


def two_milestones(price: int) -> list[Milestone]:
    return [Milestone(milestone_id="m1", title="a", amount=hkd(price // 2)),
            Milestone(milestone_id="m2", title="b", amount=hkd(price - price // 2))]


def make_hire(proposal=None, profile=None, quote=None) -> HireRequest:
    return HireRequest(proposal=proposal or make_proposal(), profile=profile or make_profile(), quote=quote or make_quote())


def make_message(text="Hello, could you quote?", **overrides) -> Message:
    fields = dict(message_id="out-1", platform="simulated", freelancer_id="f_ok", direction="outbound",
                  text=text, untrusted=False, sent_at=NOW)
    fields.update(overrides)
    return Message(**fields)


@pytest.fixture
def state() -> PolicyState:
    return PolicyState()
