#!/usr/bin/env python3
"""Standalone verifier for GigGuard decision logs. Needs only the Python stdlib and PyNaCl.

Usage:
    python verify_log.py LOG.jsonl --pubkey-file agent.pub [--expect-head HASH]
    python verify_log.py LOG.jsonl --pubkey <hex>      [--expect-head HASH]

Get the agent public key from a channel you trust, never from the log itself: whoever can
rewrite the log could also swap a key stored inside it.

For every line, in order, it checks:
  1. the line is a JSON object with exactly the expected fields
  2. seq counts up from 0 with no gaps            (catches deleted or reordered entries)
  3. prev_hash equals the previous entry_hash     (catches deleted or reordered entries)
  4. policy_hash is the same on every entry       (catches entries spliced in from another run)
  5. entry_hash == sha256(canonical JSON of the hashed fields)   (catches edited fields)
  6. signature is a valid Ed25519 signature of entry_hash        (catches forged or swapped signatures)
It stops at the first broken entry and says which one.

Tail truncation: deleting the LAST entries leaves a shorter log that is still a valid chain.
Only a head hash pinned somewhere else (on screen, in the deck, in a receipt) can catch that,
so pass it with --expect-head.

Exit code 0 = PASS, 1 = FAIL, 2 = usage or I/O error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import NamedTuple, Optional

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

GENESIS_PREV_HASH = "0" * 64
HASHED_FIELDS = ("seq", "timestamp", "prev_hash", "policy_hash", "event_type", "inputs", "rule_id", "decision", "reason")
ALL_FIELDS = set(HASHED_FIELDS) | {"entry_hash", "signature"}


class Result(NamedTuple):
    ok: bool
    message: str
    checked: int                    # entries that passed every check
    broken_seq: Optional[int]       # seq (position) of the first broken entry, if any
    head: Optional[str]             # entry_hash of the last valid entry


def canonical_json(obj) -> bytes:
    # Must match backend/app/ledger/chainlog.py exactly: sorted keys, no whitespace, UTF-8.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _has_float(obj) -> bool:
    if isinstance(obj, float):
        return True
    if isinstance(obj, dict):
        return any(_has_float(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_float(v) for v in obj)
    return False


def verify_lines(lines, pubkey_hex: str, expect_head: Optional[str] = None) -> Result:
    try:
        vk = VerifyKey(bytes.fromhex(pubkey_hex.strip()))
    except Exception:
        return Result(False, "public key is not a valid hex Ed25519 key", 0, None, None)

    prev_hash, policy_hash, count = GENESIS_PREV_HASH, None, 0

    def broken(why: str) -> Result:
        return Result(False, f"entry {count} (line {count + 1}): {why}", count, count,
                      prev_hash if count else None)

    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            return broken("not valid JSON")
        if not isinstance(entry, dict) or set(entry) != ALL_FIELDS:
            return broken("missing or unexpected fields")
        if _has_float(entry["inputs"]):
            return broken("contains a float (money must be integer minor units)")
        if entry["seq"] != count:
            return broken(f"expected seq {count}, found {entry['seq']} (entry deleted or reordered)")
        if entry["prev_hash"] != prev_hash:
            return broken("prev_hash does not match the previous entry (entry deleted or reordered)")
        if policy_hash is None:
            policy_hash = entry["policy_hash"]
        elif entry["policy_hash"] != policy_hash:
            return broken("policy_hash differs from the first entry (entry from another log)")
        recomputed = hashlib.sha256(canonical_json({k: entry[k] for k in HASHED_FIELDS})).hexdigest()
        if recomputed != entry["entry_hash"]:
            return broken("entry_hash does not match the contents (a field was edited)")
        try:
            vk.verify(bytes.fromhex(entry["entry_hash"]), bytes.fromhex(entry["signature"]))
        except (BadSignatureError, ValueError, TypeError):
            return broken("signature is not valid for this public key")
        prev_hash = entry["entry_hash"]
        count += 1

    if count == 0:
        return Result(False, "log is empty", 0, None, None)
    if expect_head is not None and expect_head.strip().lower() != prev_hash:
        return Result(False, f"head hash {prev_hash[:16]}... does not match the pinned head "
                             f"{expect_head.strip()[:16]}... (entries missing from the end, or a different log)",
                      count, None, prev_hash)
    pinned = " and matches the pinned head" if expect_head else " (head not pinned: tail truncation would not be detected)"
    return Result(True, f"{count} entries, chain intact, all signatures valid{pinned}. head={prev_hash}",
                  count, None, prev_hash)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Verify a GigGuard signed, hash-chained log.")
    p.add_argument("log", type=Path, help="exported log, one JSON entry per line")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pubkey", help="agent Ed25519 public key, hex")
    g.add_argument("--pubkey-file", type=Path, help="file containing the hex public key")
    p.add_argument("--expect-head", help="entry_hash the last entry must have (detects tail truncation)")
    args = p.parse_args(argv)

    try:
        pubkey = args.pubkey or args.pubkey_file.read_text()
        lines = args.log.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    result = verify_lines(lines, pubkey, args.expect_head)
    print(f"{'PASS' if result.ok else 'FAIL'}: {result.message}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
