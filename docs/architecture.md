# Architecture

## Trust boundaries

```
 untrusted                     LLM (advisory only)            trusted, deterministic
 ─────────────                 ───────────────────            ──────────────────────
 freelancer profiles  ──►  fit score 0-10 (strict JSON)  ──►  agent ranks by price (code)
 freelancer replies   ──►  (not read by LLM for money)        policy/quotes.py parses price
                           intro draft (no money allowed)     policy/gate.py decides
                                                              executor runs only on ALLOW
                                                              ledger signs everything
```

The LLM can make the agent pick a worse candidate or write a clumsy intro. It cannot change a
price, a fee, a limit, or whether money moves: those paths have no LLM in them.

## Modules

| Module | Job | Key idea |
|---|---|---|
| `money.py` | HKD cents helpers | ints only; fees round up |
| `observed/` | load `data/observed.json` | the only source of fees; SIMULATED entries must say so |
| `policy/mandate.py` | mandate model + Ed25519 signing | gate trusts the caller's user key, not the one inside the mandate |
| `policy/quotes.py` | read a price from text | exactly one HKD amount, no other currency, or it's not a quote |
| `policy/rules.py` | rule table: id, outcome, detail keys | the only place reasons are written; wrong keys raise |
| `policy/gate.py` | `evaluate()`, `recheck_before_release()`, ... | pure; first failing rule decides; any exception becomes R0 deny |
| `ledger/chainlog.py` | append-only signed log in SQLite | entry_hash = sha256(canonical JSON); signature = Ed25519(entry_hash); triggers block UPDATE/DELETE |
| `llm/` | SDK wrapper | disk cache, offline replay, stub mode; every failure is `LLMError` |
| `marketplace/` | SIMULATED freelancers | scripted replies, stub negotiation, or Haiku personas |
| `escrow/` | SIMULATED orders + ledger entries | fee from observed.json only; milestones funded, released one by one |
| `contracts.py` | shared data shapes | Policy, Mandate, Profile, Message, HireProposal, Decision, Order, LedgerEntry, LogEntry |
| `agent/executor.py` | the only door to side effects | gate, log decision + templated reason, then act; supplies profile and quote evidence |
| `agent/__init__.py` | orchestration | search, score, message, counter once, hire cheapest allowed |
| `harness/` | scenario runner | scenario clock + demo keys = reproducible head hash |
| `api/` | FastAPI + SSE | streams ledger entries as they're appended |
| `verifier/verify_log.py` | offline verification | stdlib + PyNaCl; reports the first broken entry; `--expect-head` |

## The gate (`backend/app/policy/gate.py`)

All functions are pure: no I/O, no clock, no LLM. Any exception becomes a deny (R0).

| Function | Question it answers |
|---|---|
| `evaluate(policy, state, proposal, now)` | May the agent make this hire? `proposal` is a `HireRequest`: the HireProposal plus the marketplace Profile and the freelancer's quote message. |
| `evaluate_message(policy, state, message, now)` | May the agent send this message? |
| `recheck_before_release(policy, state, now)` | Right before money moves, is the approved hire still OK? |
| `verify_mandate(signed, trusted_pubkey)` | Did the user really sign this policy? Checked once, before the agent acts. |

### Rules

| Id | Rule | Outcome | Boundary |
|---|---|---|---|
| R1 | budget cap | deny | committed + total (price + fees, FX rounded up) `<=` cap; exactly the cap is allowed |
| R2 | platform allow-list | deny | |
| R3 | category allow-list | deny | must be allowed by the policy *and* offered by the freelancer |
| R4 | freelancer eligibility | deny | blocked, rating `>=` min, orders `>=` min, verified if required |
| R5 | milestone-only | deny | milestones must sum to the price; `milestone_only` needs at least 2 |
| R6 | no off-platform payment | deny | when `forbid_offplatform` |
| R7 | expiry | deny | valid from `issued_at` (inclusive) to `expires_at` (exclusive) |
| R8 | revocation | deny | revoked at or before now, or superseded by a newer policy version |
| R9 | velocity | deny | at most N orders in (now − M min, now] |
| R10 | price changed | deny | re-check only: the freelancer's latest price differs from the approved one, or is unclear |
| R11 | hire limit | deny | |
| R12 | auto-approve limit | escalate | total above the limit; expires after `escalation_ttl_seconds` |
| R13 | quote unverified | escalate | price must be the one HKD price in the freelancer's message |
| R14 | proposal invalid | deny | profile or quote is for someone else; price not positive |
| R15 | currency / FX | deny | |
| R16 | message limits | deny | outbound only, length, per-freelancer count |
| R17 | mandate signature | deny | |
| R0 | input invalid | deny | malformed input or any internal error |

Hire check order: R7, R8, R14, R2, R3, R4, R6, R15, R5, R13, R11, R9, R1, R12, then `OK_HIRE`.

Every reason is rendered from the rule table, e.g. `Rule R1 | cap=80000 | total=92000 | committed=0 | DENY`.
Money is shown in raw minor units, exactly as compared.

## A hire is a transaction (time-of-check vs time-of-use)

```
evaluate()  ──allow──►  confirm price with freelancer  ──►  recheck_before_release()  ──allow──►  escrow holds money
   (check)                (user may revoke; freelancer                (use)
                           may change the price)
```

Between approval and payment the executor keeps a `PendingRelease` in the state. The re-check
denies if the policy was revoked (R8) or expired (R7), if the freelancer restated a different or
unclear price (R10), or if committed spend changed so the hire no longer fits (R1). On deny no
money moves and the agent moves on to the next offer.

## Evidence the agent cannot fake

For a hire, the executor (not the agent) supplies what the gate checks:

- the **Profile** comes from the marketplace's own record, so the agent cannot claim a better rating;
- the **quote** is the freelancer's latest inbound Message from the executor's inbox, so the agent
  cannot hire at a price nobody wrote.

The agent uses the same `eligibility_failures()` function as the gate to pre-screen candidates,
so ineligible or blocked freelancers are never contacted, and the gate still re-checks at hire time.

## The decision log

Every entry is hashed over its canonical JSON and signed with the agent key; each entry also
carries the previous entry's hash and the policy hash. The SQLite table has triggers that abort
any UPDATE or DELETE. `verifier/verify_log.py` re-checks everything offline and reports the
**first** broken entry:

| Tampering | Caught by |
|---|---|
| edit any field | `entry_hash` no longer matches the contents |
| delete or reorder entries | `seq` gap, and `prev_hash` no longer links |
| delete and renumber `seq` | `prev_hash` no longer links |
| swap in another entry's signature, or re-sign with another key | signature check against the trusted public key |
| splice in an entry from another run | `policy_hash` differs |
| **cut entries off the end** | **nothing in the log itself.** See below. |

### Tail truncation needs a pinned head

If someone deletes the last N entries, what remains is still a perfectly valid chain: every
hash links and every signature verifies. The log alone cannot prove that nothing came after.
It is only detectable if the **head hash** (the `entry_hash` of the last entry) was recorded
somewhere the attacker can't change. We pin it on screen (the dashboard's verify panel), in the
deck, and in `<scenario>.head` files from `make demo`. Check against it with:

```bash
python verifier/verify_log.py LOG.jsonl --pubkey-file agent.pub --expect-head <pinned head>
```

Without `--expect-head`, a PASS says so: "head not pinned: tail truncation would not be detected".

`make tamper-demo` runs the real verifier on an honest log (PASS) and on edited, deleted,
reordered, re-signed and truncated copies (FAIL).

## Keys

| Key | Signs | Who must trust the public key |
|---|---|---|
| user | the mandate | the gate (passed in by the caller) |
| agent | every log entry | anyone verifying a log (get `agent.pub` out of band) |

`make setup` (or `make keys`) generates both key pairs into `data/keys/` (private keys mode 0600,
git-ignored) and exports `agent.pub` and `user.pub`. The tamper demo signs with this agent key.
`GIGGUARD_KEYS=demo` (the default for the harness and API) instead derives keys from public
strings so demo runs reproduce byte for byte. Those protect nothing. `GIGGUARD_KEYS=file` uses
the generated keys.

A verifier who takes the public key from inside the log proves nothing, since anyone able to
rewrite the log could also swap that key. That is why `verify_log.py` requires `--pubkey` or
`--pubkey-file`.

## Not built yet

- Human approval flow for ESCALATE. Today the run stops and records `escalation_requested`
  with its expiry; `escalation_expired()` exists, but nothing calls it yet.
- Revocation from the UI. The executor has `revoke()` and the `revoked_mid_transaction` scenario
  uses it, but there is no API endpoint or button yet.
- Milestone release on delivery (the escrow supports it; the agent never triggers it).
- FX: the gate converts prices with an observed rate, but quotes are only read in HKD, so a
  non-HKD price always escalates. observed.json has no real FX rates yet.
- Recording a live LLM cache for the demo scenarios (needs an API key).
