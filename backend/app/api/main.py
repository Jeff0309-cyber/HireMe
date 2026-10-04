"""HTTP API for the dashboard.

  GET  /api/health                     mode, models, public keys
  GET  /api/scenarios                  list scenarios
  POST /api/runs {"scenario_id"}       start a run in a background thread
  GET  /api/runs/{id}                  run status and summary
  GET  /api/runs/{id}/events           server-sent events: one per log entry, then "done"
  GET  /api/runs/{id}/log.jsonl        the signed log, for offline verification
  GET  /api/runs/{id}/verify[?tamper=1] run verifier/verify_log.py on it (tamper=1 flips one
                                        field in a copy first, to show detection)

The main app (/ and /hire/*) lives in app/api/hire_web.py; the scripted mission demo in app/api/web.py.

Runs live in memory plus data/runs/<id>/. Everything shown is SIMULATED.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from app.api.hire_web import router as hire_router
from app.api.web import router as web_router
from app.harness import RunResult, load_scenarios, run_scenario
from app.ledger.keys import demo_key, load_or_create_key
from app.llm import agent_model, fast_model, load_env_file, mode_from_env

load_env_file()   # picks up ANTHROPIC_API_KEY / GIGGUARD_LLM_MODE from .env if present
from verify_log import verify_lines

RUNS_DIR = Path(__file__).resolve().parents[3] / "data" / "runs"
STEP_DELAY_S = int(os.environ.get("GIGGUARD_STEP_MS", "250")) / 1000  # slows runs so the UI can follow

app = FastAPI(title="HireMe (SIMULATED)")
app.include_router(hire_router)  # the main app at /: vision -> register -> search -> confirm -> pay
app.include_router(web_router)   # the scripted mission demo at /mission-demo


def _keys():
    """GIGGUARD_KEYS=demo (default, reproducible, public) or file (random keys in data/keys/)."""
    if os.environ.get("GIGGUARD_KEYS", "demo") == "file":
        return load_or_create_key("user"), load_or_create_key("agent"), "file"
    return demo_key("user"), demo_key("agent"), "demo"


@dataclass
class Run:
    id: str
    scenario_id: str
    entries: list[dict] = field(default_factory=list)
    done: bool = False
    error: Optional[str] = None
    result: Optional[RunResult] = None


RUNS: dict[str, Run] = {}


class StartRun(BaseModel):
    scenario_id: str


@app.get("/api/health")
def health():
    user, agent, key_mode = _keys()
    return {"llm_mode": mode_from_env(), "agent_model": agent_model(), "fast_model": fast_model(),
            "key_mode": key_mode, "agent_pubkey": agent.verify_key.encode().hex(),
            "user_pubkey": user.verify_key.encode().hex(), "label": "SIMULATED"}


@app.get("/api/scenarios")
def scenarios():
    return [{"id": s.id, "title": s.title, "description": s.description, "mandate": s.mandate,
             "freelancer_count": len(s.freelancers)} for s in load_scenarios().values()]


@app.post("/api/runs")
def start_run(body: StartRun):
    all_scenarios = load_scenarios()
    if body.scenario_id not in all_scenarios:
        raise HTTPException(404, "unknown scenario")
    run = Run(id=uuid.uuid4().hex[:12], scenario_id=body.scenario_id)
    RUNS[run.id] = run
    threading.Thread(target=_execute, args=(run, all_scenarios[body.scenario_id]), daemon=True).start()
    return {"run_id": run.id}


def _execute(run: Run, scenario) -> None:
    run_dir = RUNS_DIR / run.id
    run_dir.mkdir(parents=True, exist_ok=True)
    user, agent, _ = _keys()

    def on_append(entry: dict) -> None:
        run.entries.append(entry)
        time.sleep(STEP_DELAY_S)

    try:
        run.result = run_scenario(scenario, llm_mode=mode_from_env(), db_path=str(run_dir / "ledger.sqlite"),
                                  on_append=on_append, user_key=user, agent_key=agent)
        (run_dir / "log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in run.result.entries))
        (run_dir / "agent.pub").write_text(run.result.agent_pubkey + "\n")
    except Exception as exc:  # noqa: BLE001 - surface as a failed run, never as success
        run.error = type(exc).__name__
    finally:
        run.done = True


def _get(run_id: str) -> Run:
    if run_id not in RUNS:
        raise HTTPException(404, "unknown run")
    return RUNS[run_id]


@app.get("/api/runs/{run_id}")
def run_status(run_id: str):
    run = _get(run_id)
    out = {"run_id": run.id, "scenario_id": run.scenario_id, "done": run.done, "error": run.error,
           "entries": len(run.entries)}
    if run.result:
        s = run.result.summary
        out.update(status=s.status, hired=s.hired, spent_minor=s.spent_minor,
                   expectations_passed=run.result.passed, failures=run.result.failures)
    return out


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    run = _get(run_id)

    async def stream():
        sent = 0
        while True:
            while sent < len(run.entries):
                yield f"event: entry\ndata: {json.dumps(run.entries[sent])}\n\n"
                sent += 1
            if run.done and sent >= len(run.entries):
                yield f"event: done\ndata: {json.dumps(run_status(run_id))}\n\n"
                return
            await asyncio.sleep(0.05)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/log.jsonl", response_class=PlainTextResponse)
def run_log(run_id: str):
    return "".join(json.dumps(e) + "\n" for e in _get(run_id).entries)


@app.get("/api/runs/{run_id}/verify")
def run_verify(run_id: str, tamper: bool = False):
    run = _get(run_id)
    if not run.done or not run.result:
        raise HTTPException(409, "run not finished")
    entries = [json.loads(json.dumps(e)) for e in run.result.entries]
    tampered_seq = None
    if tamper:
        # Flip the first gate decision we can find, as an attacker editing the log would.
        for e in entries:
            if e["decision"] is not None:
                e["decision"] = "allow" if e["decision"] != "allow" else "deny"
                tampered_seq = e["seq"]
                break
    result = verify_lines([json.dumps(e) for e in entries], run.result.agent_pubkey, expect_head=run.result.head_hash)
    return {"ok": result.ok, "message": result.message, "checked": result.checked, "broken_seq": result.broken_seq,
            "head": run.result.head_hash, "pubkey": run.result.agent_pubkey, "tampered_seq": tampered_seq}
