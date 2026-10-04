"""python -m app.harness [--export DIR] [scenario_id ...]

Runs scenarios, prints a pass/fail table, and exits 1 if any scenario fails.
With --export, writes <DIR>/<scenario>.jsonl, <scenario>.head (the head hash to pin) and agent.pub,
so the logs can be checked with verifier/verify_log.py on any machine.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.harness import load_scenarios, run_scenario
from app.llm import mode_from_env
from app.money import format_hkd


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("scenarios", nargs="*")
    p.add_argument("--export", type=Path)
    args = p.parse_args(argv)

    scenarios = load_scenarios()
    ids = args.scenarios or list(scenarios)
    mode = mode_from_env()
    print(f"HireMe harness | LLM mode: {mode} | marketplace and payments: SIMULATED\n")

    failed = 0
    for sid in ids:
        result = run_scenario(scenarios[sid], llm_mode=mode)
        mark = "PASS" if result.passed else "FAIL"
        s = result.summary
        print(f"[{mark}] {sid:<22} status={s.status:<9} hired={str(s.hired):<10} "
              f"spent={format_hkd(s.spent_minor):<14} entries={len(result.entries):<3} head={result.head_hash[:12]}")
        for f in result.failures:
            print(f"        - {f}")
        failed += not result.passed
        if args.export:
            args.export.mkdir(parents=True, exist_ok=True)
            (args.export / f"{sid}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in result.entries))
            (args.export / f"{sid}.head").write_text(result.head_hash + "\n")   # pin this to catch truncation
            (args.export / "agent.pub").write_text(result.agent_pubkey + "\n")

    print(f"\n{len(ids) - failed}/{len(ids)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
