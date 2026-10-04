# Data contracts

`backend/app/contracts.py` defines the shapes of the data that moves through HireMe. They are
Pydantic v2 models with **no business logic**. A contract answers "is this well-formed?". Whether
an action is *allowed* is decided by the policy gate.

Tests: `tests/test_contracts.py`.

## Rules every model follows

- **Unknown fields are rejected.** A field like `"approved_by_admin": true` makes validation fail,
  so nothing can smuggle in extra meaning.
- **Models are immutable** once built. To change one, make a new one.
- **No floats anywhere.** Money is integer cents, ratings are stars × 100, FX rates are × 1,000,000.
  The string `"100"`, the float `100.0` and `True` are all refused where an integer is expected.
- **Timestamps must have a timezone**, e.g. `2026-10-01T09:00:00Z`. A timestamp without one is refused.
- **Platform ids** are lowercase slugs like `simulated`. All platforms are simulated (hard rule 7).

The few validators that do exist only reject data that contradicts itself (see each model).
Cross-field business rules, such as whether the milestones add up to the price or the price is
under the cap, belong in the policy gate, not here.

---

## Money

An amount and its currency.

| Field | Meaning |
|---|---|
| `amount_minor` | Whole number of cents, 0 or more. HKD 12.50 is `1250`. |
| `currency` | Three-letter ISO code, upper case. Defaults to `HKD`. |

Negative amounts are not allowed. Direction (money in or out) is expressed by the record that
holds the Money, e.g. a `LedgerEntry` of kind `refund`.

### FxQuote (part of HireProposal)

A currency conversion rate. `rate_micros` is the rate × 1,000,000, so 1 USD = 7.8 HKD is
`7800000`. `source_id` must point at an entry in `data/observed.json` (hard rule 4), and
`observed_at` says when it was observed. The two currencies must differ.

### Fee (part of HireProposal)

One named fee line (e.g. `service_fee`) with an amount and the `observed.json` entry it came from.

---

## Policy

The limits the user sets: everything the agent is allowed to do. It sits inside a `Mandate`,
which the user signs, and the gate enforces it.

| Field | Meaning |
|---|---|
| `policy_id`, `version` | Which policy this is. Version starts at 1 and goes up on each change. |
| `issued_at`, `expires_at` | The policy allows nothing before `issued_at` or from `expires_at` on. `issued_at` must come first. |
| `budget_cap` | The most the agent may spend under this policy, fees included. |
| `auto_approve_limit` | Optional. A single hire costing more than this needs a human (escalate). Same currency as the cap. |
| `max_hires` | How many freelancers may be hired (default 1). |
| `allowed_platforms` | Marketplaces the agent may use (at least one). |
| `allowed_categories` | Kinds of work it may hire for (at least one). |
| `blocked_freelancers` | Freelancer ids the agent may not message or hire. |
| `eligibility.min_rating_x100` | Lowest acceptable rating, stars × 100 (0–500). 4.5 stars = `450`. |
| `eligibility.min_completed_orders` | Lowest acceptable number of finished jobs. |
| `eligibility.verified_required` | Only hire freelancers the platform has verified. |
| `milestone_only` | If true, payment must be split into at least two milestones, never all up front. |
| `forbid_offplatform` | If true (the default), never pay outside the platform. |
| `velocity` | At most `max_orders` orders in any `window_minutes` window. Stops a runaway agent from placing order after order. |
| `max_messages_per_freelancer`, `max_message_chars` | Messaging limits (defaults 3 and 800). |
| `escalation_ttl_seconds` | How long a "needs a human" request waits. If nobody answers in time, it counts as denied. |

## Mandate and SignedMandate

A **Mandate** is what the user signs: who they are (`principal`), what they want done (`task`),
the job's `category` (which must be one of the policy's `allowed_categories`), and the `policy`.

A **SignedMandate** wraps it with the user's Ed25519 `signature` over the mandate's canonical
JSON, plus `signer_pubkey` for information. The gate does not trust `signer_pubkey`. It checks
the signature against the user key it was given separately, so swapping both the policy and the
key inside the file does not work. Changing any field after signing breaks the signature.

## Profile

A freelancer as the marketplace shows them: name, categories, skills, `rating_x100`,
`completed_orders`, `verified`, and their own `profile_text`.

`untrusted` is always `true` and cannot be set to anything else, because the freelancer wrote the
text (hard rule 6). `label` is always `SIMULATED`.

## Scorecard

The agent's assessment of one candidate: `fit_score` (0–100), whether they are `eligible`, which
eligibility checks they failed (`eligibility_failures`, e.g. `R4.min_rating`, `R4.blocked`), their `quoted_price`, the
`total_cost` including fees, and their `rank`.

Self-consistency check: `eligible` is true exactly when `eligibility_failures` is empty.

## Message

One message in a conversation with a freelancer.

| Field | Meaning |
|---|---|
| `direction` | `outbound` (agent → freelancer) or `inbound` (freelancer → agent). |
| `text` | The message body. |
| `untrusted` | Whether the text may contain hostile instructions. |
| `sent_at` | When it was sent. |

Self-consistency check: an `inbound` message must have `untrusted: true`. Our own outbound
messages may be marked trusted.

## HireProposal

What the agent wants to do: hire `freelancer_id` on `platform` for work in `category`, at
`price`, plus any `fees`, an optional `fx` conversion, an optional list of `milestones`, and a
`payment_channel`:

- `platform_escrow`: the platform holds the money until delivery
- `platform_direct`: paid through the platform without escrow
- `off_platform`: paid outside the platform (normally forbidden by `forbid_offplatform`)

A proposal is a request, not a commitment. It only becomes an `Order` if the gate allows it.

## Milestone

One stage of the work (`title`), with its `amount`, an optional `due_at`, and a `status`:
`pending` → `funded` (money held) → `released` (paid) or `refunded`.

## Decision

The gate's answer to an action.

| Field | Meaning |
|---|---|
| `outcome` | `allow`, `deny`, or `escalate` (ask a human). |
| `rule_id` | The rule that decided, e.g. `BUDGET_CAP`. |
| `details` | Facts behind the decision, as strings, integers, booleans or null. No floats and no nesting, so a decision serialises the same way every time. |

`rule_id` is a stable id from the rule table in `policy/rules.py` (`R1`…`R17`, `R0`, or an
`OK_…` id for allows). Each rule declares exactly which detail keys it uses.

The human-readable reason is not stored here. `render_reason()` builds it from the rule table
(hard rule 5), and the executor logs it next to the decision:
`Rule R1 | cap=80000 | total=92000 | committed=0 | DENY`. Money is shown in raw minor units.

## Order

A hire that went ahead: which `proposal_id` it came from, the `freelancer_id` and `platform`, the
`total` committed, its `milestones`, and a `status`: `created`, `in_progress`, `delivered`,
`completed`, `cancelled` or `disputed`. Always labelled `SIMULATED`.

## LedgerEntry vs LogEntry

Similar names, different jobs:

| | **LedgerEntry** | **LogEntry** |
|---|---|---|
| Records | money movements | decisions and events |
| Example | "held HKD 1,575 for order o1" | "gate denied hire of f_cy: rating too low" |
| Signed / chained | no (it is data inside log entries) | yes |

### LedgerEntry

One SIMULATED money movement in escrow: `kind` is `hold`, `release`, `refund` or `fee`, with the
`amount`, the `order_id` (and `milestone_id` if any), and the time `at`.

### LogEntry

One line of the signed, hash-chained audit log, in exactly the format
`verifier/verify_log.py` checks:

| Field | Meaning |
|---|---|
| `seq` | Position in the log, counting from 0. |
| `timestamp` | When the entry was written. |
| `prev_hash` | `entry_hash` of the previous entry (64 zeros for the first). |
| `policy_hash` | SHA-256 of the signed mandate bytes. The same on every entry, so entries can't be moved between logs. |
| `event_type` | What happened, e.g. `gate_decision`, `message_received`, `order_created`. |
| `inputs` | The facts behind the event. Floats are rejected at any depth. |
| `rule_id` | For gate decisions, the rule that decided (e.g. `R1`); otherwise null. |
| `decision` | `allow`, `deny` or `escalate`; otherwise null. |
| `reason` | The templated reason, e.g. `Rule R1 \| cap=80000 \| total=92000 \| committed=0 \| DENY`; otherwise null. |
| `entry_hash` | SHA-256 over the canonical JSON (sorted keys, no whitespace, UTF-8) of all fields above. |
| `signature` | Ed25519 signature of the 32 raw `entry_hash` bytes by the agent key (128 hex chars). |

`rule_id`, `decision` and `reason` are either all set (a gate decision) or all null.
The contract only checks the shape. Checking that hashes chain and signatures are valid is the
verifier's job.
