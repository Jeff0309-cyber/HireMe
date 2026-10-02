# GigGuard

An AI agent that finds, messages and hires a freelancer for you, but can only spend what your
signed mandate allows. A deterministic policy gate checks every action, and every step goes into
a signed, hash-chained log that anyone can verify offline.

**Everything is SIMULATED.** The marketplace, freelancers, fees and escrow are synthetic. No real
marketplace is called and no money moves.

## Quick start

```bash
make setup      # Python 3.11 venv + deps (needs uv), frontend deps, Ed25519 key pairs
make test       # gate rules, property tests, contracts, ledger tampering, agent, API
make demo       # run every scenario, export signed logs, verify them with the standalone verifier
make run        # API on :8000
make frontend   # dashboard on :5173 (second terminal)
```

No API key is needed: the default `GIGGUARD_LLM_MODE=stub` uses a deterministic stand-in for the
model. To use Claude:

```bash
GIGGUARD_LLM_MODE=live ANTHROPIC_API_KEY=... make harness     # records responses to data/llm_cache/
GIGGUARD_LLM_MODE=offline make harness                         # replays them, no network
```

## How a run works

1. The user signs a **mandate**: the task plus a policy (budget, auto-approve limit, eligibility,
   velocity, milestone-only, no off-platform payment, expiry). Shapes live in `backend/app/contracts.py`.
2. The agent searches the SIMULATED marketplace. The LLM scores profile fit (0–10) and drafts an intro.
3. Every message and hire goes through the **gate** (`backend/app/policy/gate.py`), pure functions
   returning allow / deny / escalate with a stable rule id (R1–R17) and a templated reason like
   `Rule R1 | cap=80000 | total=92000 | committed=0 | DENY`.
4. Prices are read from freelancer replies by a **regex parser**, never by the LLM. A message with
   no price, two prices, or another currency is ignored.
5. The gate re-parses the freelancer's own message at hire time, so the agent cannot hire at a price
   the freelancer did not write.
6. A hire is approved, confirmed with the freelancer, then **re-checked right before payment**
   (`recheck_before_release`), so a revocation or price change in between stops the money.
7. Each step is appended to the **ledger**: SHA-256 chained and Ed25519-signed.

## Scenarios (`scenarios/`)

| Scenario | What it shows |
|---|---|
| happy_path | 4 freelancers fail R4 and are never contacted; hire confirmed, re-checked, paid in 2 milestones |
| prompt_injection | instructions in profiles and replies are ignored; a double-price reply is dropped |
| over_budget | HKD 1,200 already committed; every quote breaks R1 even after a counter-offer |
| needs_human | quote within budget but above the auto-approve limit: R12 escalates (expires after 24 h) |
| price_changed | freelancer raises the price after approval: R10 cancels before payment, next offer is hired |
| revoked_mid_transaction | user revokes right after approval: R8 stops the payment and everything after |
| velocity_limit | 2 orders already in the last hour: R9 denies every new hire |
| expired_mandate | R7 denies every action |
| tampered_mandate | budget raised after signing: R17, the agent does nothing |

## Verify a log yourself

```bash
make tamper-demo     # PASS on an honest log, then FAIL on edited / deleted / reordered / re-signed copies
```

```bash
python verifier/verify_log.py data/demo/happy_path.jsonl --pubkey-file data/demo/agent.pub --expect-head "$(cat data/demo/happy_path.head)"
```

The verifier needs only Python and PyNaCl, and reports the first broken entry. Deleting entries
from the **end** of a log can only be detected against a head hash pinned somewhere else (on
screen, in the deck), which is what `--expect-head` checks.

See [docs/architecture.md](docs/architecture.md) for the trust boundaries, log format and key handling.
