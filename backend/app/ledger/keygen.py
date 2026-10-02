"""python -m app.ledger.keygen [--dir data/keys]

Creates the agent key (signs log entries) and the user key (signs mandates) if they don't exist,
and exports their public keys as <name>.pub. Private keys are written with mode 0600 and are
git-ignored. Run by `make setup` / `make keys`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.ledger.keys import DEFAULT_KEY_DIR, load_or_create_key


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", type=Path, default=DEFAULT_KEY_DIR)
    args = p.parse_args(argv)
    for name in ("agent", "user"):
        existed = (args.dir / f"{name}.key").exists()
        key = load_or_create_key(name, args.dir)
        print(f"{name:<6} {'kept' if existed else 'created'}  public key -> {args.dir / f'{name}.pub'}  "
              f"{key.verify_key.encode().hex()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
