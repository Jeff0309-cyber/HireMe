"""Signed, hash-chained, append-only decision log.

Each entry:

    seq          0, 1, 2, ... with no gaps
    timestamp    ISO 8601, from the injected clock
    prev_hash    entry_hash of the previous entry (64 zeros for the first)
    policy_hash  sha256 of the signed mandate bytes, the same on every entry
    event_type   what happened, e.g. "gate_decision", "message_received"
    inputs       the facts behind the event (JSON object, no floats)
    rule_id      gate rule id, or null for events that are not decisions
    decision     "allow" | "deny" | "escalate", or null
    reason       rendered from the rule table, or null. Callers cannot pass free text in.
    entry_hash   sha256 over the canonical JSON of every field above
    signature    Ed25519 over the 32 raw bytes of entry_hash, by the agent key

Canonical JSON = sorted keys, no whitespace, UTF-8. The verifier (verifier/verify_log.py)
re-implements exactly this, independently.

SQLite triggers abort every UPDATE and DELETE, so even code with a database handle cannot
rewrite history through this table. (Someone with the file can still edit it by hand; that is
what the hashes and signatures are for.)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from nacl.signing import SigningKey

from app.contracts import Decision, LogEntry
from app.policy import render_reason

GENESIS_PREV_HASH = "0" * 64
HASHED_FIELDS = ("seq", "timestamp", "prev_hash", "policy_hash", "event_type", "inputs", "rule_id", "decision", "reason")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    seq        INTEGER PRIMARY KEY,
    entry_hash TEXT NOT NULL UNIQUE,
    json       TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'chainlog is append-only: UPDATE is not allowed'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'chainlog is append-only: DELETE is not allowed'); END;
"""


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8. Floats are rejected so money can never be a float."""
    _reject_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _reject_floats(obj: Any) -> None:
    if isinstance(obj, float):
        raise TypeError("floats are not allowed in the log")
    if isinstance(obj, dict):
        for v in obj.values():
            _reject_floats(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _reject_floats(v)


def compute_entry_hash(entry: dict) -> str:
    return hashlib.sha256(canonical_json({k: entry[k] for k in HASHED_FIELDS})).hexdigest()


class ChainLog:
    def __init__(self, signing_key: SigningKey, clock: Callable[[], str], policy_hash: str,
                 db_path: str = ":memory:", on_append: Optional[Callable[[dict], None]] = None):
        self._key = signing_key
        self._clock = clock                  # returns an ISO timestamp string
        self.policy_hash = policy_hash
        self._on_append = on_append
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    @property
    def public_key_hex(self) -> str:
        return self._key.verify_key.encode().hex()

    def append(self, event_type: str, inputs: dict, decision: Optional[Decision] = None) -> dict:
        """Add one entry. For gate decisions pass the Decision; rule_id, decision and reason come from it."""
        with self._lock:
            last = self._db.execute("SELECT json FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
            prev = json.loads(last[0]) if last else None
            entry: dict[str, Any] = {
                "seq": prev["seq"] + 1 if prev else 0,
                "timestamp": self._clock(),
                "prev_hash": prev["entry_hash"] if prev else GENESIS_PREV_HASH,
                "policy_hash": self.policy_hash,
                "event_type": event_type,
                "inputs": inputs,
                "rule_id": decision.rule_id if decision else None,
                "decision": decision.outcome if decision else None,
                "reason": render_reason(decision) if decision else None,
            }
            entry["entry_hash"] = compute_entry_hash(entry)
            entry["signature"] = self._key.sign(bytes.fromhex(entry["entry_hash"])).signature.hex()
            LogEntry.model_validate(entry)   # every entry must match the LogEntry contract
            self._db.execute("INSERT INTO entries (seq, entry_hash, json) VALUES (?, ?, ?)",
                             (entry["seq"], entry["entry_hash"], json.dumps(entry, ensure_ascii=False)))
            self._db.commit()
        if self._on_append:
            self._on_append(entry)
        return entry

    def entries(self) -> list[dict]:
        rows = self._db.execute("SELECT json FROM entries ORDER BY seq").fetchall()
        return [json.loads(r[0]) for r in rows]

    @property
    def head_hash(self) -> str:
        """entry_hash of the last entry. Pin it somewhere public to make truncation detectable."""
        row = self._db.execute("SELECT entry_hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS_PREV_HASH

    def export_jsonl(self, path: Path) -> None:
        Path(path).write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in self.entries()),
                              encoding="utf-8")
