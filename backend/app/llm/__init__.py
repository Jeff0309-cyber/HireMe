"""Thin, deterministic wrapper around the Anthropic SDK.

Three modes (env GIGGUARD_LLM_MODE):
- "live":    call the API, then cache the response on disk by sha256(model + system + prompt).
             A cache hit never calls the API, so a recorded run replays identically.
- "offline": replay the cache only. A cache miss raises LLMError (callers fail closed).
- "stub":    no model at all; a deterministic Python function answers. Used by tests, the
             harness and the default demo so everything runs without an API key.

Determinism notes:
- Haiku 4.5 accepts temperature, so we send temperature=0.
- Claude Opus 5.5 rejects sampling parameters (temperature/top_p/top_k return HTTP 400), so
  for that model the disk cache is what makes runs reproducible.

Any API error, refusal, or empty answer raises LLMError. Nothing here touches money.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Literal, Optional

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = ROOT / "data" / "llm_cache"


def load_env_file(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines from .env (git-ignored) into the environment, without overriding
    variables that are already set. Keeps the API key out of code, commands and chat."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
Mode = Literal["live", "offline", "stub"]
StubFn = Callable[[str, str, str], str]   # (task, system, prompt) -> text

# Models that still accept `temperature`. Newer models (Opus 5.5, Opus 5, Sonnet 5, ...) reject it.
_SAMPLING_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6", "claude-sonnet-4-5")


def agent_model() -> str:
    return os.environ.get("GIGGUARD_MODEL", "claude-opus-5-5")


def fast_model() -> str:
    return os.environ.get("GIGGUARD_FAST_MODEL", "claude-haiku-4-5-20251001")


def mode_from_env() -> Mode:
    mode = os.environ.get("GIGGUARD_LLM_MODE", "stub")
    if mode not in ("live", "offline", "stub"):
        raise ValueError(f"GIGGUARD_LLM_MODE must be live, offline or stub, not {mode!r}")
    return mode  # type: ignore[return-value]


class LLMError(Exception):
    """Raised for every LLM failure. Callers must treat it as 'deny / skip', never 'allow'."""


def cache_key(model: str, system: str, prompt: str) -> str:
    return hashlib.sha256(json.dumps([model, system, prompt]).encode()).hexdigest()


class LLMClient:
    def __init__(self, mode: Mode, cache_dir: Path = DEFAULT_CACHE_DIR,
                 stub: Optional[StubFn] = None, sdk_client: Any = None):
        self.mode = mode
        self.cache_dir = Path(cache_dir)
        self._stub = stub
        self._sdk = sdk_client          # injectable for tests; created lazily in live mode
        self.calls = 0                  # API calls actually made (cache hits don't count)

    def complete(self, task: str, model: str, system: str, prompt: str, max_tokens: int = 16000) -> str:
        """Return the model's text answer. `task` names the call site (used by stub mode)."""
        if self.mode == "stub":
            if self._stub is None:
                raise LLMError("stub mode without a stub function")
            return self._stub(task, system, prompt)

        key = cache_key(model, system, prompt)
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text())["response"]
        if self.mode == "offline":
            raise LLMError(f"offline mode: no cached response for {task} ({key[:12]})")

        text = self._call_api(model, system, prompt, max_tokens)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": model, "system": system, "prompt": prompt,
                                    "task": task, "response": text}, indent=1))
        return text

    def create_message(self, task: str, params: dict[str, Any]) -> dict[str, Any]:
        """One Messages API call with full params (tools, history...), cached on disk by a hash
        of the params. Returns the response as a plain dict. Used by the tool-using agent."""
        if self.mode == "stub":
            raise LLMError("the AI agent needs GIGGUARD_LLM_MODE=live (or offline with a recorded cache)")
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / f"msg-{key}.json"
        if path.exists():
            return json.loads(path.read_text())["response"]
        if self.mode == "offline":
            raise LLMError(f"offline mode: no cached response for {task} ({key[:12]})")

        import anthropic
        if self._sdk is None:
            self._sdk = anthropic.Anthropic()
        try:
            self.calls += 1
            response = self._sdk.messages.create(**params)
        except anthropic.APIError as exc:
            raise LLMError(f"API error: {type(exc).__name__}") from exc
        data = response.model_dump(mode="json", exclude_none=True)
        if data.get("stop_reason") in ("refusal", "max_tokens"):
            raise LLMError(f"model stopped with {data.get('stop_reason')}")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"task": task, "params": params, "response": data}, indent=1))
        return data

    def _call_api(self, model: str, system: str, prompt: str, max_tokens: int) -> str:
        import anthropic  # imported here so stub/offline modes don't need credentials

        if self._sdk is None:
            self._sdk = anthropic.Anthropic()
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if model.startswith(_SAMPLING_PREFIXES):
            params["temperature"] = 0
        else:
            # Opus 5.5 defaults to medium effort; say so explicitly so behaviour is pinned.
            params["output_config"] = {"effort": "medium"}
        try:
            self.calls += 1
            response = self._sdk.messages.create(**params)
        except anthropic.APIError as exc:
            raise LLMError(f"API error: {type(exc).__name__}") from exc

        if response.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(f"model stopped with {response.stop_reason}")
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
        if not text:
            raise LLMError("empty response")
        return text
