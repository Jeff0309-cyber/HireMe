"""Property tests: whatever the numbers, the gate never allows spending above the cap."""
from datetime import timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from app.contracts import FxQuote, Money
from app.policy import PendingRelease, PolicyState, evaluate, proposal_total_minor, recheck_before_release
from conftest import NOW, hkd, make_hire, make_policy, make_proposal, make_quote

money = st.integers(min_value=0, max_value=10_000_000)


@settings(max_examples=500, deadline=None)
@given(cap=money, auto=st.one_of(st.none(), money), price=money, fee=money, committed=money,
       prior_orders=st.integers(0, 3), max_hires=st.integers(1, 4), max_orders=st.integers(1, 4))
def test_evaluate_never_allows_total_above_cap(cap, auto, price, fee, committed, prior_orders, max_hires, max_orders):
    policy = make_policy(budget_cap=hkd(cap), auto_approve_limit=None if auto is None else hkd(auto),
                         max_hires=max_hires, velocity={"max_orders": max_orders, "window_minutes": 60})
    req = make_hire(proposal=make_proposal(price=price, fee=fee),
                    quote=make_quote(f"Price HKD {price // 100}.{price % 100:02d}"))
    state = PolicyState(committed_minor=committed,
                        order_times=[NOW - timedelta(minutes=i + 1) for i in range(prior_orders)])
    d = evaluate(policy, state, req, NOW)

    if d.outcome == "allow":
        assert committed + price + fee <= cap              # R1, the property that matters
        assert d.details["total"] == price + fee
        assert auto is None or price + fee <= auto
        assert prior_orders < max_hires and prior_orders < max_orders


@settings(max_examples=500, deadline=None)
@given(cap=money, committed=money, approved_total=money, price=st.integers(1, 10_000_000),
       current=st.one_of(st.none(), money))
def test_recheck_never_allows_total_above_cap(cap, committed, approved_total, price, current):
    pending = PendingRelease(proposal=make_proposal(price=price), approved_total_minor=approved_total,
                             current_price_minor=current, approved_at=NOW)
    d = recheck_before_release(make_policy(budget_cap=hkd(cap)), PolicyState(committed_minor=committed, pending=pending), NOW)
    if d.outcome == "allow":
        assert committed + approved_total <= cap
        assert current == price


@settings(max_examples=300, deadline=None)
@given(amount=st.integers(0, 10**9), fee=st.integers(0, 10**9), rate=st.integers(1, 10**9))
def test_fx_total_never_rounds_down(amount, fee, rate):
    fx = FxQuote(from_currency="USD", to_currency="HKD", rate_micros=rate, source_id="x", observed_at=NOW)
    p = make_proposal(fx=fx).model_copy(update={
        "price": Money(amount_minor=amount, currency="USD"),
        "fees": [make_proposal().fees[0].model_copy(update={"amount": Money(amount_minor=fee, currency="USD")})]})
    total = proposal_total_minor(p)
    # Exact value is (amount + fee) * rate / 1e6; each part rounds up, so total is never below it.
    assert total * 1_000_000 >= (amount + fee) * rate


@settings(max_examples=300, deadline=None)
@given(st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=20),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=12), children, max_size=6),
    max_leaves=20,
))
def test_gate_never_raises_and_never_allows_garbage(junk):
    assert evaluate(make_policy(), PolicyState(), junk, NOW).outcome == "deny"


@settings(max_examples=200, deadline=None)
@given(text=st.text(max_size=200))
def test_arbitrary_quote_text_never_allows_unmatched_price(text):
    d = evaluate(make_policy(), PolicyState(), make_hire(quote=make_quote(text)), NOW)
    if d.outcome == "allow":
        assert "1,000" in text or "1000" in text
