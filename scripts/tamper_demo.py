#!/usr/bin/env python3
"""Tamper demo: the real verifier says PASS on an honest log, then FAIL on every kind of edit.

    make tamper-demo

1. Runs the happy_path scenario, signing the log with the agent key from `make keys`.
2. Exports the log, its public key, and its head hash (the value we pin on screen / in the deck).
3. Runs verifier/verify_log.py, as a separate process, on the honest log and on tampered copies.
Exits 0 only if the honest log passes and every tampered copy fails.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "verifier")]

from app.harness import load_scenarios, run_scenario  # noqa: E402
from app.ledger.keys import load_or_create_key  # noqa: E402

OUT = ROOT / "data" / "tamper_demo"
VERIFIER = ROOT / "verifier" / "verify_log.py"
GREEN, RED, BOLD, RESET = "\033[32m", "\033[31m", "\033[1m", "\033[0m"


def write(name: str, entries: list[dict]) -> Path:
    path = OUT / f"{name}.jsonl"
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")
    return path


def verify(path: Path, pub: Path, head: str | None) -> tuple[int, str]:
    cmd = [sys.executable, str(VERIFIER), str(path), "--pubkey-file", str(pub)]
    if head:
        cmd += ["--expect-head", head]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    result = run_scenario(load_scenarios()["happy_path"], agent_key=load_or_create_key("agent"))
    entries = result.entries
    pub = OUT / "agent.pub"
    pub.write_text(result.agent_pubkey + "\n")
    head = result.head_hash
    (OUT / "head.txt").write_text(head + "\n")

    order = next(i for i, e in enumerate(entries) if e["event_type"] == "order_created")
    hire = next(i for i, e in enumerate(entries) if e["rule_id"] == "OK_HIRE")

    edited = json.loads(json.dumps(entries))
    edited[order]["inputs"]["order"]["total"]["amount_minor"] = 15_750          # HKD 1,575 -> HKD 157.50
    deleted = entries[:hire] + entries[hire + 1:]                                # hide the hire decision
    reordered = list(entries)
    reordered[hire], reordered[hire + 1] = reordered[hire + 1], reordered[hire]
    swapped = json.loads(json.dumps(entries))
    swapped[hire]["signature"] = entries[hire + 1]["signature"]                  # a real signature, wrong entry
    truncated = entries[:hire]                                                   # drop everything from the hire on

    cases = [
        ("Honest log", write("original", entries), head, True),
        (f"Edit one field (order total in entry {order})", write("edited", edited), head, False),
        (f"Delete an entry (entry {hire}, the hire decision)", write("deleted", deleted), head, False),
        (f"Reorder entries {hire} and {hire + 1}", write("reordered", reordered), head, False),
        (f"Swap a signature onto entry {hire}", write("swapped_signature", swapped), head, False),
        ("Cut off the tail, head NOT pinned", write("truncated", truncated), None, True),
        ("Cut off the tail, head pinned (--expect-head)", OUT / "truncated.jsonl", head, False),
    ]

    print(f"{BOLD}HireMe tamper demo{RESET}  (SIMULATED run: happy_path, {len(entries)} entries)")
    print(f"public key : {result.agent_pubkey}")
    print(f"pinned head: {head}\n")

    all_as_expected = True
    for label, path, expect_head, should_pass in cases:
        code, output = verify(path, pub, expect_head)
        passed = code == 0
        as_expected = passed == should_pass
        all_as_expected &= as_expected
        colour = GREEN if passed else RED
        note = "" if as_expected else f"  {RED}<-- UNEXPECTED{RESET}"
        if label.startswith("Cut off the tail, head NOT"):
            note += "  (expected: truncation is invisible without a pinned head)"
        print(f"{BOLD}{label}{RESET}{note}\n  {colour}{output}{RESET}\n")

    print(f"{BOLD}{'All results as expected.' if all_as_expected else 'SOME RESULTS WERE NOT AS EXPECTED.'}{RESET}")
    print(f"Files: {OUT.relative_to(ROOT)}/  (try: python verifier/verify_log.py "
          f"{(OUT / 'edited.jsonl').relative_to(ROOT)} --pubkey-file {pub.relative_to(ROOT)})")
    return 0 if all_as_expected else 1


if __name__ == "__main__":
    sys.exit(main())
