"""Vision -> brief -> register -> search -> confirm -> deliver -> release."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.agent.brief import draft_brief
from app.agent.hiring import FlowError, HiringSession, RegisterForm
from app.agent.messaging import flag_inbound
from app.ledger.keys import demo_key
from app.llm import LLMClient
from app.marketplace.catalog import CATEGORIES, catalog, gig, reply_to_inquiry
from app.policy import parse_quote
from verify_log import verify_lines

LOGO = "I want a logo for my new bubble tea shop in Sham Shui Po. Budget HKD 2,000, within 7 days. It must work in Chinese and English."
COPY = "We need 300-400 words of menu descriptions for our cafe, up to HKD 1,500, in two weeks."


def clock_from(start=datetime(2026, 10, 1, 9, tzinfo=timezone.utc)):
    t = [start]

    def clock():
        t[0] += timedelta(seconds=1)
        return t[0]
    return clock


def session() -> HiringSession:
    return HiringSession(clock_from(), demo_key("user"), demo_key("agent"))


def form_from(brief, **kw) -> RegisterForm:
    data = dict(title=brief.title, category=brief.category, budget_hkd=str(brief.budget_minor // 100),
                deadline_days=brief.deadline_days, min_words=brief.min_words, max_words=brief.max_words)
    data.update(kw)
    return RegisterForm(**data)


# --- brief ----------------------------------------------------------------------------------------

def test_logo_vision():
    b = draft_brief(LOGO)
    assert (b.category, b.budget_minor, b.deadline_days, b.source) == ("logo_design", 200_000, 7, "rules")
    assert b.title.startswith("A logo for my new bubble tea shop")
    assert not b.warnings


def test_writing_vision_reads_word_range_and_weeks():
    b = draft_brief(COPY)
    assert (b.category, b.budget_minor, b.deadline_days, b.min_words, b.max_words) == ("copywriting", 150_000, 14, 300, 400)


@pytest.mark.parametrize("text, warning", [
    ("Edit 5 Instagram reels from my footage", "No budget"),
    ("Edit 5 Instagram reels from my footage", "No deadline"),
    ("Translate my menu, budget $500, 3 days", "Only HKD"),
    ("Logo please. HKD 500 or HKD 900, 3 days", "Several HKD amounts"),
    ("Something nice for my shop, HKD 500, 3 days", "Couldn't tell"),
])
def test_missing_or_unclear_fields_are_left_for_the_user(text, warning):
    assert any(warning in w for w in draft_brief(text).warnings)


def test_budget_cue_picks_the_right_amount():
    b = draft_brief("Logo for my shop. Competitors paid HKD 5,000 but my budget is HKD 1,200. 5 days.")
    assert b.budget_minor == 120_000


def test_empty_vision_rejected():
    with pytest.raises(ValueError):
        draft_brief("   ")


class _FakeLive(LLMClient):
    def __init__(self, reply):
        super().__init__("live")
        self.reply = reply

    def complete(self, task, model, system, prompt, max_tokens=16000):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_claude_cannot_invent_a_budget_or_deadline():
    reply = json.dumps({"title": "Bubble tea logo", "category": "logo_design", "keywords": ["logo"],
                        "budget_hkd": 9000, "deadline_days": 30, "min_words": None, "max_words": None, "must_haves": []})
    b = draft_brief(LOGO, _FakeLive(reply))
    assert b.source == "claude" and b.title == "Bubble tea logo"
    assert b.budget_minor == 200_000 and b.deadline_days == 7          # the user's numbers, not Claude's
    assert any("isn't written in your text" in w for w in b.warnings)


def test_claude_budget_kept_when_the_user_wrote_it():
    reply = json.dumps({"title": "Logo", "category": "logo_design", "keywords": [], "budget_hkd": "2,000",
                        "deadline_days": 7, "min_words": None, "max_words": None, "must_haves": ["bilingual"]})
    b = draft_brief(LOGO, _FakeLive(reply))
    assert (b.budget_minor, b.deadline_days, b.must_haves) == (200_000, 7, ["bilingual"])


@pytest.mark.parametrize("reply", ["not json", '{"title": 5}', '{"title": "x", "category": "logo_design", "evil": 1}',
                                   RuntimeError("down")])
def test_bad_claude_reply_falls_back_to_rules(reply):
    from app.llm import LLMError
    reply = LLMError("down") if isinstance(reply, RuntimeError) else reply
    b = draft_brief(LOGO, _FakeLive(reply))
    assert b.source == "rules" and b.budget_minor == 200_000
    assert "simpler keyword reader" in b.warnings[0]


# --- catalog ----------------------------------------------------------------------------------------

def test_catalog_is_deterministic_and_simulated():
    gigs = catalog()
    assert len(gigs) == 48 and len({g.gig_id for g in gigs}) == 48
    assert {g.label for g in gigs} == {"SIMULATED"}
    assert all("(SIMULATED)" in g.seller.display_name for g in gigs)
    assert {g.category for g in gigs} == set(CATEGORIES)
    assert all(isinstance(p.price_minor, int) and p.price_minor > 0 for g in gigs for p in g.packages)


@pytest.mark.parametrize("g", catalog(), ids=lambda g: g.gig_id)
def test_replies_are_safe_to_read(g):
    reply = reply_to_inquiry(g, "Basic")
    q = parse_quote(reply)
    if g.persona == "honest":
        assert q.status == "ok" and q.amount_minor == g.package("Basic").price_minor and not flag_inbound(reply)
    elif g.persona == "upseller":
        assert q.status == "ambiguous"                                   # two prices: never auto-accepted
    else:
        assert flag_inbound(reply)                                       # off-platform / fake authority


def test_hostile_listings_are_flagged():
    for g in catalog():
        if g.persona in ("offplatform", "injection"):
            assert flag_inbound(g.description), g.gig_id


# --- the flow ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("vision", [LOGO, COPY])
def test_full_flow(vision):
    s = session()
    b = s.draft(vision)
    s.register(form_from(b))
    s.search()
    assert s.stage == "proposed" and s.escrow.ledger == []               # nothing paid before confirmation
    p = s.proposal
    assert p.total_minor <= b.budget_minor and p.precheck["outcome"] == "allow"
    picked = gig(p.gig_id)
    assert picked.persona == "honest" and not flag_inbound(picked.description)
    assert s.confirm()["rule_id"] == "OK_HIRE" and s.stage == "funded"
    assert s.deliver()["ok"] and s.stage == "delivered"
    assert s.release()["rule_id"] == "OK_RELEASE" and s.stage == "released"
    assert s.escrow.released_minor() == p.price_minor and s.escrow.held_minor() == 0
    entries = s.log.entries()
    r = verify_lines([json.dumps(e) for e in entries], s.log.public_key_hex, expect_head=s.log.head_hash)
    assert r.ok, r.message
    kinds = [e["event_type"] for e in entries]
    assert kinds.index("user_confirmed") < kinds.index("escrow_transition")      # money moved only after the user said yes


def test_agent_never_picks_flagged_or_ineligible_sellers():
    for cat, c in CATEGORIES.items():
        s = session()
        s.draft(f"a {cat} job")
        s.register(RegisterForm(title=f"{cat} job", category=cat, budget_hkd="5000", deadline_days=14,
                                min_words=200 if c.deliverable == "writing" else None,
                                max_words=400 if c.deliverable == "writing" else None))
        s.search()
        for a in s.assessments:
            if a.status == "picked":
                g = gig(a.gig_id)
                assert g.persona == "honest" and not a.excluded_because
            if a.status == "contacted_rejected":
                assert a.rejected_because


def test_upseller_is_skipped_after_messaging():
    s = session()
    s.draft("video job")
    s.register(RegisterForm(title="Edit reels", category="video_editing", budget_hkd="3000", deadline_days=10))
    s.search()
    skipped = [a for a in s.assessments if a.status == "contacted_rejected"]
    assert skipped and skipped[0].rejected_because == "UNCLEAR_PRICE"
    assert any(e["event_type"] == "seller_skipped" for e in s.log.entries())


def test_tiny_budget_finds_no_match_and_spends_nothing():
    s = session()
    b = s.draft(LOGO)
    s.register(form_from(b, budget_hkd="50"))
    s.search()
    assert s.stage == "no_match" and s.proposal is None and s.escrow.ledger == []
    assert all("OVER_BUDGET" in a.excluded_because or a.excluded_because for a in s.assessments)


def test_milestones_when_asked():
    s = session()
    b = s.draft(LOGO)
    s.register(form_from(b, milestone_only=True))
    s.search()
    assert len(s.proposal.milestones) == 2 and sum(s.proposal.milestones) == s.proposal.price_minor
    assert s.confirm()["outcome"] == "allow"


@pytest.mark.parametrize("step", ["search", "confirm", "deliver", "release"])
def test_steps_out_of_order_are_refused(step):
    with pytest.raises(FlowError):
        getattr(session(), step)()


def test_cannot_release_before_delivery_or_confirm_twice():
    s = session()
    s.register(form_from(s.draft(LOGO)))
    s.search()
    s.confirm()
    with pytest.raises(FlowError):
        s.release()
    with pytest.raises(FlowError):
        s.confirm()


@pytest.mark.parametrize("kw", [{"budget_hkd": "0"}, {"category": "crypto"}, {"min_words": 500, "max_words": 100}])
def test_register_rejects_bad_forms(kw):
    s = session()
    b = s.draft(LOGO)
    with pytest.raises(FlowError):
        s.register(form_from(b, **kw))


def test_register_form_rejects_float_like_garbage():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RegisterForm(title="Logo", category="logo_design", budget_hkd="2,000.505", deadline_days=7)
    with pytest.raises(ValidationError):
        RegisterForm(title="Logo", category="logo_design", budget_hkd="1e5", deadline_days=7)


# --- deadlines in months and other everyday phrasings ---------------------------------------------

@pytest.mark.parametrize("phrase, days", [
    ("Within 1 month", 30), ("in a month", 30), ("In 2 to 6 months", 60), ("within a fortnight", 14),
    ("by next week", 7), ("need it tomorrow", 1), ("in two weeks", 14), ("10 days", 10),
])
def test_deadline_phrasings(phrase, days):
    assert draft_brief(f"Wedding video, HKD 8,000. {phrase}").deadline_days == days


def test_minutes_are_not_a_deadline():
    b = draft_brief("A 3-5 minute highlight reel, HKD 8,000")
    assert b.deadline_days is None


def test_wedding_conversation_from_the_bug_report():
    # The user's real answers: the AI read "Within 1 month" as 30 days; the parser now agrees.
    from app.agent.brief import brief_from_model
    user_text = ("I want a wedding videographer\n"
                 "What style of wedding video are you looking for: Cinematic / Storytelling\n"
                 "What is your estimated budget for this project: Under HKD 10,000\n"
                 "When is the wedding scheduled or when do you need the final video: Within 1 month\n"
                 "What is the desired length of the final edited video: 3-5 minute highlight reel")
    b = brief_from_model({"title": "Wedding video", "category": "video_editing", "budget_hkd": 10000,
                          "deadline_days": 30}, user_text, source="gemini")
    assert (b.deadline_days, b.budget_minor) == (30, 1_000_000)
    assert not b.warnings


def test_ai_deadline_without_user_words_gives_one_clear_warning():
    from app.agent.brief import brief_from_model
    b = brief_from_model({"title": "Logo", "category": "logo_design", "deadline_days": 30},
                         "A logo for my shop, HKD 2,000", source="gemini")
    assert b.deadline_days is None
    deadline_warnings = [w for w in b.warnings if "deadline" in w.lower()]
    assert len(deadline_warnings) == 1 and "suggested about 30 days" in deadline_warnings[0]
