"""Ed25519 key handling.

Two kinds of key:
- The agent key signs log entries. Its public key is what verifiers need.
- The user key signs the mandate. The gate trusts only this public key.

Keys live in data/keys/ (git-ignored) as hex seed files. For the harness and demo we also
derive DEMO keys from a fixed public string so runs are byte-for-byte reproducible. Demo keys
are public knowledge and must never protect anything real.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from nacl.signing import SigningKey

DEFAULT_KEY_DIR = Path(__file__).resolve().parents[3] / "data" / "keys"


def demo_key(name: str) -> SigningKey:
    """Deterministic DEMO key. Anyone can recreate it, so it proves nothing outside demos."""
    return SigningKey(hashlib.sha256(f"gigguard-demo-key:{name}".encode()).digest())


def load_or_create_key(name: str, key_dir: Path = DEFAULT_KEY_DIR) -> SigningKey:
    """Load data/keys/<name>.key, or create it with a fresh random seed (file mode 0600)."""
    key_dir.mkdir(parents=True, exist_ok=True)
    path = key_dir / f"{name}.key"
    if path.exists():
        return SigningKey(bytes.fromhex(path.read_text().strip()))
    key = SigningKey.generate()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.encode().hex())
    (key_dir / f"{name}.pub").write_text(key.verify_key.encode().hex() + "\n")
    return key
