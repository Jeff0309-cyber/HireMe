import json
import time

import pytest
from fastapi.testclient import TestClient

from app.api import main as api
from app.harness import load_scenarios, run_scenario
from app.harness.__main__ import main as harness_main

SCENARIOS = load_scenarios()


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_scenario_meets_expectations(sid):
    result = run_scenario(SCENARIOS[sid])
    assert result.passed, result.failures


def test_runs_are_reproducible():
    a = run_scenario(SCENARIOS["happy_path"])
    b = run_scenario(SCENARIOS["happy_path"])
    assert a.head_hash == b.head_hash and a.entries == b.entries


def test_harness_cli_exports_verifiable_logs(tmp_path, capsys):
    assert harness_main(["happy_path", "--export", str(tmp_path)]) == 0
    assert (tmp_path / "happy_path.jsonl").exists() and (tmp_path / "agent.pub").exists()
    assert len((tmp_path / "happy_path.head").read_text().strip()) == 64
    assert "1/1 scenarios passed" in capsys.readouterr().out


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "STEP_DELAY_S", 0)
    monkeypatch.setattr(api, "RUNS_DIR", tmp_path)
    return TestClient(api.app)


def _wait(client, run_id):
    for _ in range(200):
        status = client.get(f"/api/runs/{run_id}").json()
        if status["done"]:
            return status
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_api_end_to_end(client):
    health = client.get("/api/health").json()
    assert health["label"] == "SIMULATED" and health["llm_mode"] == "stub"
    assert {s["id"] for s in client.get("/api/scenarios").json()} == set(SCENARIOS)

    run_id = client.post("/api/runs", json={"scenario_id": "happy_path"}).json()["run_id"]
    status = _wait(client, run_id)
    assert status["status"] == "hired" and status["expectations_passed"]

    with client.stream("GET", f"/api/runs/{run_id}/events") as r:
        body = "".join(r.iter_text())
    assert body.count("event: entry") == status["entries"] and "event: done" in body

    good = client.get(f"/api/runs/{run_id}/verify").json()
    assert good["ok"] and good["checked"] == status["entries"]
    bad = client.get(f"/api/runs/{run_id}/verify", params={"tamper": True}).json()
    assert not bad["ok"] and bad["tampered_seq"] is not None
    assert bad["broken_seq"] == bad["tampered_seq"]          # the verifier points at the edited entry
    last = json.loads(client.get(f"/api/runs/{run_id}/log.jsonl").text.strip().splitlines()[-1])
    assert good["head"] == last["entry_hash"]

    assert client.get(f"/api/runs/{run_id}/log.jsonl").text.count("\n") == status["entries"]


def test_api_unknown_ids(client):
    assert client.post("/api/runs", json={"scenario_id": "nope"}).status_code == 404
    assert client.get("/api/runs/nope").status_code == 404
