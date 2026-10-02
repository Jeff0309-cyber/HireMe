"""ChainLog + standalone verifier: honest logs pass, every kind of tampering fails at the right entry."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.ledger import GENESIS_PREV_HASH, HASHED_FIELDS, ChainLog, canonical_json, compute_entry_hash
from app.ledger.keys import demo_key, load_or_create_key
from app.policy import make_decision
from verify_log import HASHED_FIELDS as VERIFIER_FIELDS
from verify_log import canonical_json as verifier_canonical_json
from verify_log import verify_lines

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "verifier" / "verify_log.py"
KEY = demo_key("test-agent")
PUB = KEY.verify_key.encode().hex()
POLICY_HASH = "ab" * 32


def _log(n=5, db_path=":memory:", policy_hash=POLICY_HASH) -> ChainLog:
    ticks = iter(range(10_000))
    log = ChainLog(KEY, lambda: f"2026-10-01T09:{next(ticks):02d}:00Z", policy_hash=policy_hash, db_path=db_path)
    for i in range(n):
        if i % 2:
            log.append("gate_decision", {"amount_minor": 100 * i},
                       make_decision("R1", cap=80_000, total=92_000, committed=0))
        else:
            log.append("event", {"i": i, "amount_minor": 100 * i, "text": "héllo"})
    return log


def _lines(entries):
    return [json.dumps(e, ensure_ascii=False) for e in entries]


def _copy(entries):
    return json.loads(json.dumps(entries))


# --- format ---------------------------------------------------------------------------------

def test_entry_has_exactly_the_specified_fields():
    e = _log(2).entries()[1]
    assert set(e) == {"seq", "timestamp", "prev_hash", "policy_hash", "event_type", "inputs", "rule_id",
                      "decision", "reason", "entry_hash", "signature"}
    assert (e["rule_id"], e["decision"]) == ("R1", "deny")
    assert e["reason"] == "Rule R1 | cap=80000 | total=92000 | committed=0 | DENY"


def test_non_decision_entries_have_null_decision_fields():
    e = _log(1).entries()[0]
    assert e["rule_id"] is None and e["decision"] is None and e["reason"] is None


def test_chain_links_hashes_and_signatures():
    entries = _log().entries()
    assert entries[0]["prev_hash"] == GENESIS_PREV_HASH
    assert all(b["prev_hash"] == a["entry_hash"] for a, b in zip(entries, entries[1:]))
    assert all(e["entry_hash"] == compute_entry_hash(e) for e in entries)
    assert all(e["policy_hash"] == POLICY_HASH for e in entries)
    KEY.verify_key.verify(bytes.fromhex(entries[0]["entry_hash"]), bytes.fromhex(entries[0]["signature"]))


def test_canonical_json_is_sorted_and_compact_and_matches_the_verifier():
    obj = {"b": 1, "a": {"d": [1, 2], "c": "é"}}
    assert canonical_json(obj) == b'{"a":{"c":"\xc3\xa9","d":[1,2]},"b":1}'
    assert canonical_json(obj) == verifier_canonical_json(obj)
    assert HASHED_FIELDS == VERIFIER_FIELDS


def test_floats_rejected():
    with pytest.raises(TypeError):
        canonical_json({"amount": 1.5})
    with pytest.raises(TypeError):
        _log(0).append("event", {"nested": [{"x": 0.1}]})


def test_honest_log_passes():
    log = _log()
    r = verify_lines(_lines(log.entries()), PUB, expect_head=log.head_hash)
    assert r.ok and r.checked == 5 and r.head == log.head_hash, r.message


# --- tampering: each must fail, at the right entry ---------------------------------------------

def _edit_field(entries):
    entries[2]["inputs"]["amount_minor"] = 1
    return entries, 2


def _edit_decision(entries):
    entries[1]["decision"] = "allow"
    return entries, 1


def _edit_reason(entries):
    entries[3]["reason"] = "Rule R1 | looks fine | ALLOW"
    return entries, 3


def _delete(entries):
    del entries[2]
    return entries, 2


def _delete_and_renumber(entries):
    # A smarter attacker fixes the seq numbers too; prev_hash still breaks.
    del entries[2]
    for i, e in enumerate(entries):
        e["seq"] = i
    return entries, 2


def _reorder(entries):
    entries[1], entries[3] = entries[3], entries[1]
    return entries, 1


def _swap_signature(entries):
    entries[2]["signature"] = entries[3]["signature"]       # a genuine signature, but for another entry
    return entries, 2


def _resign_with_other_key(entries):
    other = demo_key("attacker")
    entries[4]["inputs"]["amount_minor"] = 0
    entries[4]["entry_hash"] = compute_entry_hash(entries[4])
    entries[4]["signature"] = other.sign(bytes.fromhex(entries[4]["entry_hash"])).signature.hex()
    return entries, 4


def _splice_other_policy(entries):
    foreign = _log(5, policy_hash="cd" * 32).entries()
    entries[3] = foreign[3]
    return entries, 3


@pytest.mark.parametrize("attack", [_edit_field, _edit_decision, _edit_reason, _delete, _delete_and_renumber,
                                    _reorder, _swap_signature, _resign_with_other_key, _splice_other_policy],
                         ids=lambda f: f.__name__.lstrip("_"))
def test_tampering_fails_at_first_broken_entry(attack):
    tampered, expected_seq = attack(_copy(_log().entries()))
    r = verify_lines(_lines(tampered), PUB)
    assert not r.ok
    assert r.broken_seq == expected_seq, r.message
    assert r.message.startswith(f"entry {expected_seq} ")


def test_tail_truncation_needs_a_pinned_head():
    log = _log()
    truncated = log.entries()[:3]
    assert verify_lines(_lines(truncated), PUB).ok                     # undetectable on its own
    r = verify_lines(_lines(truncated), PUB, expect_head=log.head_hash)
    assert not r.ok and "pinned head" in r.message


def test_wrong_key_and_garbage():
    entries = _log().entries()
    assert not verify_lines(_lines(entries), demo_key("someone").verify_key.encode().hex()).ok
    assert not verify_lines(_lines(entries), "zz").ok
    assert verify_lines(["{not json"], PUB).broken_seq == 0
    assert not verify_lines([], PUB).ok
    extra = _copy(entries)
    extra[0]["note"] = "added"
    assert verify_lines(_lines(extra), PUB).broken_seq == 0


# --- SQLite: append-only ---------------------------------------------------------------------------

def test_sqlite_triggers_block_update_and_delete(tmp_path):
    db = tmp_path / "log.sqlite"
    _log(3, db_path=str(db))
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE entries SET json = '{}' WHERE seq = 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM entries WHERE seq = 2")
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3


def test_sqlite_persists_and_chain_continues_after_reopen(tmp_path):
    db = str(tmp_path / "log.sqlite")
    ChainLog(KEY, lambda: "2026-10-01T09:00:00Z", POLICY_HASH, db_path=db).append("a", {"x": 1})
    again = ChainLog(KEY, lambda: "2026-10-01T09:00:01Z", POLICY_HASH, db_path=db)
    again.append("b", {"x": 2})
    out = tmp_path / "log.jsonl"
    again.export_jsonl(out)
    r = verify_lines(out.read_text(encoding="utf-8").splitlines(), PUB, expect_head=again.head_hash)
    assert r.ok and r.checked == 2


# --- CLI -------------------------------------------------------------------------------------------

def _cli(*args):
    return subprocess.run([sys.executable, str(VERIFIER), *map(str, args)], capture_output=True, text=True)


def test_verifier_cli(tmp_path):
    log = _log()
    path = tmp_path / "log.jsonl"
    log.export_jsonl(path)
    (tmp_path / "agent.pub").write_text(PUB + "\n")

    ok = _cli(path, "--pubkey-file", tmp_path / "agent.pub", "--expect-head", log.head_hash)
    assert ok.returncode == 0 and ok.stdout.startswith("PASS"), ok.stdout + ok.stderr

    lines = path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["decision"] = "allow"
    lines[1] = json.dumps(second)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bad = _cli(path, "--pubkey", PUB)
    assert bad.returncode == 1 and bad.stdout.startswith("FAIL: entry 1 (line 2)")

    assert _cli(tmp_path / "missing.jsonl", "--pubkey", PUB).returncode == 2


def test_verifier_needs_only_stdlib_and_pynacl():
    import ast
    tree = ast.parse(VERIFIER.read_text())
    roots = {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    roots |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert roots - {"__future__"} <= set(sys.stdlib_module_names) | {"nacl"}


def test_tamper_demo_script_passes_then_fails():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "tamper_demo.py")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert out.index("PASS") < out.index("FAIL")
    assert out.count("FAIL:") == 5 and "All results as expected." in out


# --- keys ------------------------------------------------------------------------------------------

def test_load_or_create_key(tmp_path):
    k1 = load_or_create_key("agent", tmp_path)
    k2 = load_or_create_key("agent", tmp_path)
    assert k1.encode() == k2.encode()
    assert (tmp_path / "agent.key").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "agent.pub").read_text().strip() == k1.verify_key.encode().hex()


def test_keygen_cli_exports_public_keys(tmp_path):
    from app.ledger.keygen import main
    assert main(["--dir", str(tmp_path)]) == 0
    assert (tmp_path / "agent.pub").exists() and (tmp_path / "user.pub").exists()
