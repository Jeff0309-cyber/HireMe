import json

import pytest

from app.money import apply_bps, convert_micros, decimal_str_to_minor, format_hkd, format_money, require_minor
from app.observed import load_observed
from app.policy import parse_quote


# --- money ---

def test_format_and_parse_roundtrip():
    assert format_hkd(123456) == "HKD 1,234.56"
    assert format_hkd(5) == "HKD 0.05"
    assert format_hkd(-100) == "-HKD 1.00"
    assert decimal_str_to_minor("1,234.5") == 123450
    assert decimal_str_to_minor("7") == 700


@pytest.mark.parametrize("bad", [1.5, True, "100", None])
def test_require_minor_rejects_non_int(bad):
    with pytest.raises(TypeError):
        require_minor(bad)


def test_format_money_and_fx_conversion():
    assert format_money(780, "USD") == "USD 7.80"
    assert convert_micros(10_000, 7_800_000) == 78_000          # USD 100 -> HKD 780
    assert convert_micros(1, 7_800_001) == 8                    # 7.800001 rounds up
    with pytest.raises(TypeError):
        convert_micros(100, 7.8)


def test_apply_bps_rounds_up():
    assert apply_bps(100_000, 500) == 5_000
    assert apply_bps(190_476, 500) == 9_524      # 9523.8 -> 9524
    with pytest.raises(TypeError):
        apply_bps(100.0, 500)


# --- quotes ---

@pytest.mark.parametrize("text, minor", [
    ("My price is HKD 1,500.", 150_000),
    ("hk$1200.50 total", 120_050),
    ("HKD1500 all in", 150_000),
    ("Quote: HKD 1,500. That's HKD 1,500 total.", 150_000),   # same amount twice is fine
])
def test_parse_ok(text, minor):
    q = parse_quote(text)
    assert q.status == "ok" and q.amount_minor == minor


@pytest.mark.parametrize("text, status", [
    ("Happy to help!", "none"),
    ("HKD 800, plus wire HKD 40,000 deposit", "ambiguous"),
    ("USD 200 or HKD 1,500", "ambiguous"),
    ("It's $200", "ambiguous"),
    ("HKD 1,500 (about €180)", "ambiguous"),
    ("HKD 1,2345", "none"),
])
def test_parse_not_ok(text, status):
    assert parse_quote(text).status == status


# --- observed.json ---

def _write(tmp_path, entries):
    p = tmp_path / "observed.json"
    p.write_text(json.dumps({"entries": entries}))
    return p


GOOD = {"id": "x", "value": 500, "unit": "basis_points", "source_url": "simulated://x",
        "observed_at": "2026-10-01T00:00:00Z", "observer": "test", "label": "SIMULATED"}


def test_repo_observed_file_is_valid_and_labelled():
    data = load_observed()
    assert data, "observed.json must not be empty"
    for e in data.values():
        assert e.is_simulated or e.source_url.startswith("https://")


def test_observed_rejects_bad_entries(tmp_path):
    assert load_observed(_write(tmp_path, [GOOD]))["x"].value == 500
    for bad in [
        {**GOOD, "value": 5.0},                                   # float
        {k: v for k, v in GOOD.items() if k != "observer"},       # missing field
        {**GOOD, "observed_at": "last tuesday"},                  # not ISO
        {**GOOD, "source_url": "https://fiverr.com"},             # SIMULATED but real-looking url
        {**{k: v for k, v in GOOD.items() if k != "label"}},      # unlabelled but simulated:// url
    ]:
        with pytest.raises(Exception):
            load_observed(_write(tmp_path, [bad]))
    with pytest.raises(ValueError):
        load_observed(_write(tmp_path, [GOOD, GOOD]))
