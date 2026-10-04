"""Google Gemini over its REST API (generativelanguage.googleapis.com, v1beta).

We use plain HTTPS + JSON instead of the SDK so that requests and responses are plain dicts:
they cache to disk byte-for-byte (offline replay) and Gemini's "thought signatures" are sent
back exactly as received, which multi-turn tool use requires.

The API key is read from the GEMINI_API_KEY environment variable (loaded from the git-ignored
.env file). It is sent only to Google, in the x-goog-api-key header, never in a URL.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

import httpx

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Tried in order when the chosen model is temporarily overloaded (HTTP 429/500/503).
FALLBACK_MODELS = ("gemini-3.5-flash", "gemini-flash-latest")
FALLBACK_MODEL = FALLBACK_MODELS[-1]
RETRY_PAUSE_S = 1.5
HttpPost = Callable[[str, dict, dict], tuple[int, dict]]   # (url, headers, json) -> (status, json)


def gemini_model() -> str:
    return os.environ.get("GIGGUARD_GEMINI_MODEL", "gemini-3.8-flash")


def api_key() -> Optional[str]:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _httpx_post(url: str, headers: dict, body: dict) -> tuple[int, dict]:
    r = httpx.post(url, headers=headers, json=body, timeout=120)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": {"message": r.text[:200]}}


class GeminiError(Exception):
    pass


def generate(model: str, body: dict[str, Any], http_post: Optional[HttpPost] = None,
             sleep: Callable[[float], None] = time.sleep) -> tuple[str, dict]:
    """One generateContent call. Returns (model actually used, response dict).
    If a model is overloaded, waits briefly and tries the next in FALLBACK_MODELS. Raises GeminiError."""
    key = api_key()
    if not key:
        raise GeminiError("no GEMINI_API_KEY configured")
    post = http_post or _httpx_post
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    chain = list(dict.fromkeys((model, *FALLBACK_MODELS)))
    for i, m in enumerate(chain):
        try:
            status, data = post(API.format(model=m), headers, body)
        except httpx.HTTPError as exc:
            raise GeminiError(f"network error: {type(exc).__name__}") from exc
        if status in (429, 500, 503):
            if i < len(chain) - 1:
                sleep(RETRY_PAUSE_S)
                continue
            raise GeminiError("every Gemini model we tried is overloaded right now; please try again in a minute")
        if status != 200:
            raise GeminiError(f"HTTP {status}: {data.get('error', {}).get('message', '')[:160]}")
        candidates = data.get("candidates") or []
        if not candidates or "content" not in candidates[0]:
            reason = (data.get("promptFeedback") or {}).get("blockReason") or (candidates[0].get("finishReason") if candidates else "no candidates")
            raise GeminiError(f"no answer ({reason})")
        if candidates[0].get("finishReason") not in (None, "STOP", "MAX_TOKENS"):
            raise GeminiError(f"stopped: {candidates[0].get('finishReason')}")
        return m, data
    raise GeminiError("no Gemini model answered")


def parts(response: dict) -> list[dict]:
    return response["candidates"][0]["content"].get("parts", [])


def answer_text(response: dict) -> str:
    """The visible answer, without thought summaries."""
    return "".join(p.get("text", "") for p in parts(response) if not p.get("thought")).strip()


def thought_text(response: dict) -> str:
    """Gemini's thought summary (if includeThoughts was on). Model text, shown as 'thinking'."""
    return "\n".join(p.get("text", "") for p in parts(response) if p.get("thought")).strip()
