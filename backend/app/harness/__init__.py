"""Scenario harness: builds a complete simulated world from scenarios/*.json, runs the agent,
and checks the outcome against the scenario's "expect" block.

Runs are reproducible: time comes from a scenario clock (one second per log entry), keys are
DEMO keys derived from fixed strings, and the LLM is stubbed or replayed from cache. The
same scenario therefore always produces the same log, down to the head hash.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from nacl.signing import SigningKey
from pydantic import BaseModel, ConfigDict, Field

from app.agent import RunSummary, run_agent
from app.agent.executor import GatedExecutor
from app.agent.stub import stub_llm
from app.escrow import SimulatedEscrow
from app.ledger import ChainLog
from app.ledger.keys import demo_key
from app.llm import DEFAULT_CACHE_DIR, LLMClient, Mode
from app.contracts import Mandate, Policy, SignedMandate
from app.marketplace import SimFreelancer, SimulatedMarketplace
from app.observed import load_observed
from app.policy import PolicyState, mandate_digest, sign_mandate
from verify_log import verify_lines

SCENARIO_DIR = Path(__file__).resolve().parents[3] / "scenarios"


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    hired: Optional[str] = None
    max_spent_minor: Optional[int] = None
    rules_seen: list[str] = Field(default_factory=list)
    rules_not_seen: list[str] = Field(default_factory=list)


class PriorOrder(BaseModel):
    """An order already placed under this policy before the run (counts for R1 and R9)."""
    model_config = ConfigDict(extra="forbid")
    at: str
    total_minor: int


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    description: str
    now: str
    mandate: Mandate
    tamper_after_signing: dict[str, Any] = Field(default_factory=dict)   # policy fields changed after signing
    freelancers: list[SimFreelancer]
    prior_orders: list[PriorOrder] = Field(default_factory=list)
    revoke_when: Optional[Literal["hire_approved"]] = None   # the user revokes mid-transaction
    expect: Expect


def load_scenarios(directory: Path = SCENARIO_DIR) -> dict[str, Scenario]:
    out = {}
    for path in sorted(directory.glob("*.json")):
        s = Scenario.model_validate_json(path.read_text())
        out[s.id] = s
    return out


class ScenarioClock:
    """Deterministic time: starts at the scenario's `now`, advances 1s per log entry."""

    def __init__(self, start_iso: str):
        self._t = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))

    def now(self) -> datetime:
        return self._t

    def tick_iso(self) -> str:
        self._t += timedelta(seconds=1)
        return self._t.isoformat().replace("+00:00", "Z")


@dataclass
class RunResult:
    scenario_id: str
    summary: RunSummary
    entries: list[dict]
    agent_pubkey: str
    user_pubkey: str
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def head_hash(self) -> str:
        return self.entries[-1]["entry_hash"] if self.entries else ""


def run_scenario(scenario: Scenario, *, llm_mode: Mode = "stub", cache_dir: Path = DEFAULT_CACHE_DIR,
                 db_path: str = ":memory:", on_append: Optional[Callable[[dict], None]] = None,
                 user_key: Optional[SigningKey] = None, agent_key: Optional[SigningKey] = None) -> RunResult:
    user_key = user_key or demo_key("user")
    agent_key = agent_key or demo_key("agent")
    clock = ScenarioClock(scenario.now)

    # The user signs the mandate. A scenario may then tamper with it to prove the gate notices.
    signed = sign_mandate(scenario.mandate, user_key)
    if scenario.tamper_after_signing:
        signed = _tamper(signed, scenario.tamper_after_signing)

    llm = LLMClient(llm_mode, cache_dir=cache_dir, stub=stub_llm)
    # policy_hash binds every log entry to the exact mandate bytes the user signed.
    log = ChainLog(agent_key, clock.tick_iso, policy_hash=mandate_digest(signed.mandate), db_path=db_path,
                   on_append=on_append)
    state = PolicyState(
        committed_minor=sum(o.total_minor for o in scenario.prior_orders),
        order_times=[datetime.fromisoformat(o.at.replace("Z", "+00:00")) for o in scenario.prior_orders],
    )
    executor = GatedExecutor(signed, user_key.verify_key.encode().hex(),
                             SimulatedMarketplace(scenario.freelancers, llm), SimulatedEscrow(), log, clock.now,
                             state=state)
    if scenario.revoke_when:
        executor.hooks[scenario.revoke_when] = [lambda ex: ex.revoke()]
    summary = run_agent(executor, llm, load_observed())

    result = RunResult(scenario.id, summary, log.entries(), log.public_key_hex,
                       user_key.verify_key.encode().hex())
    result.failures = check_expectations(scenario, result)
    return result


def check_expectations(scenario: Scenario, result: RunResult) -> list[str]:
    exp, s = scenario.expect, result.summary
    failures = []
    if s.status != exp.status:
        failures.append(f"status {s.status} != expected {exp.status}")
    if s.hired != exp.hired:
        failures.append(f"hired {s.hired} != expected {exp.hired}")
    if exp.max_spent_minor is not None and s.spent_minor > exp.max_spent_minor:
        failures.append(f"spent {s.spent_minor} > max {exp.max_spent_minor}")
    if s.spent_minor > scenario.mandate.policy.budget_cap.amount_minor:
        failures.append("spent more than the mandate's total budget")
    seen = {e["rule_id"] for e in result.entries if e["rule_id"]}
    for rule in exp.rules_seen:
        if rule not in seen:
            failures.append(f"expected rule {rule} to fire")
    for rule in exp.rules_not_seen:
        if rule in seen:
            failures.append(f"rule {rule} fired but should not")
    check = verify_lines([json.dumps(e) for e in result.entries], result.agent_pubkey, expect_head=result.head_hash)
    if not check.ok:
        failures.append(f"log failed verification: {check.message}")
    return failures


def _tamper(signed: SignedMandate, changes: dict[str, Any]) -> SignedMandate:
    """Change policy fields but keep the old signature, as an attacker editing the mandate would."""
    policy = Policy.model_validate({**signed.mandate.policy.model_dump(), **changes})
    return signed.model_copy(update={"mandate": signed.mandate.model_copy(update={"policy": policy})})
