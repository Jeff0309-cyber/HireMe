"""Gate vs naive evaluation: statistics, scenario generators and the honest report."""
import random

import pytest

from app.harness.run import (SCENARIOS, fmt_rate, report, run_all, run_gate, run_naive, wilson)


def test_wilson_matches_known_values():
    lo, hi = wilson(0, 10)
    assert lo == 0 and hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = wilson(5, 10)
    assert (lo, hi) == (pytest.approx(0.2366, abs=1e-4), pytest.approx(0.7634, abs=1e-4))
    lo, hi = wilson(100, 100)
    assert hi == 1 and lo == pytest.approx(0.9630, abs=1e-4)
    assert wilson(0, 0) == (0.0, 0.0)
    assert fmt_rate(18, 100) == "18/100 = 18.0% [11.7, 26.7]"


def test_six_scenarios():
    names = {gen(random.Random(1)).scenario for gen in SCENARIOS}
    assert names == {"exact-cap", "one-cent-over", "add-on upsell", "off-platform request",
                     "fake pre-approval", "revoked mandate"}


@pytest.fixture(scope="module")
def results():
    return run_all(60, seed=7)


def test_gate_never_overspends_and_keeps_legit_hires(results):
    _, tallies, _, _ = results
    assert all(t.overspend == 0 for (s, arm), t in tallies.items() if arm == "gate")
    assert tallies[("exact-cap", "gate")].executed == 60
    assert tallies[("one-cent-over", "gate")].rules == {"R1": 60}
    assert tallies[("off-platform request", "gate")].rules == {"R6": 60}
    assert tallies[("revoked mandate", "gate")].executed == 0


def test_naive_baseline_executes_everything(results):
    _, tallies, _, _ = results
    assert all(t.executed == t.n for (s, arm), t in tallies.items() if arm == "naive")
    assert tallies[("one-cent-over", "naive")].overspend == 60
    assert tallies[("revoked mandate", "naive")].overspend == 60


def test_gate_injection_successes_are_all_within_budget():
    # Where an attacker's payment passes the gate, it must be because it fit every limit.
    rng = random.Random(3)
    for gen in SCENARIOS:
        for _ in range(40):
            inst = gen(rng)
            g = run_gate(inst)
            if g.executed:
                assert not g.overspend and g.rules == ["OK_HIRE", "OK_RELEASE"]
            assert run_naive(inst).executed


def test_report_is_markdown_and_reports_failures_honestly():
    md, gate_safe = report(50, seed=11)
    assert "| Scenario | Arm | n |" in md and "## Summary" in md and "## Where the gate failed" in md
    assert "attacker-requested payments went through the gate" in md       # known gap is not hidden
    assert "## Limitations" in md
    assert gate_safe


def test_report_is_reproducible():
    assert report(30, seed=5)[0] == report(30, seed=5)[0]
