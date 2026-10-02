# GigGuard. Everything is SIMULATED: no real marketplaces, no real payments.
PY      := .venv/bin/python
ENV     := PYTHONPATH=backend:verifier
NPM     := PATH="$(CURDIR)/.venv/bin:$$PATH" npm

.PHONY: setup keys test run frontend harness eval demo demo-scenarios tamper-demo

setup:            ## create .venv (Python 3.11) and install deps; needs uv
	uv venv --python 3.11 .venv
	uv pip install --python $(PY) -r pyproject.toml --extra dev
	cd frontend && $(NPM) install
	$(MAKE) keys

keys:             ## create the agent + user Ed25519 key pairs (once) and export data/keys/*.pub
	$(ENV) $(PY) -m app.ledger.keygen

test:             ## run every pytest suite
	$(ENV) $(PY) -m pytest -q

run:              ## API on :8000 (start `make frontend` in another terminal)
	$(ENV) $(PY) -m uvicorn app.api.main:app --port 8000 --reload --reload-dir backend

frontend:         ## dashboard on :5173, proxies /api to :8000
	cd frontend && $(NPM) run dev

harness:          ## run all scenarios and check expectations
	$(ENV) $(PY) -m app.harness

eval:             ## gate vs naive baseline on 6 scenarios -> markdown with Wilson 95% intervals (docs/harness_results.md)
	$(ENV) $(PY) -m app.harness.run

demo:             ## run the SIMULATED mission headlessly and print its signed log
	$(ENV) $(PY) -m app.agent.mission --export data/mission

demo-scenarios:   ## run all scenarios, export signed logs, verify them with the standalone verifier
	rm -rf data/demo
	$(ENV) $(PY) -m app.harness --export data/demo
	@for f in data/demo/*.jsonl; do printf "%-44s " $$f; $(PY) verifier/verify_log.py $$f \
		--pubkey-file data/demo/agent.pub --expect-head $$(cat $${f%.jsonl}.head); done

tamper-demo:      ## verifier says PASS on an honest log, FAIL on edited / deleted / reordered / re-signed copies
	$(ENV) $(PY) scripts/tamper_demo.py
