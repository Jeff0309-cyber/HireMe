"""Deterministic stand-in for the LLM (GIGGUARD_LLM_MODE=stub).

It answers the same prompts the real model gets, with simple rules, so tests, the harness
and the default demo run without an API key and give identical results every time.
It deliberately ignores the untrusted profile text, the way we want the real model to.
"""
from __future__ import annotations

import re

_STOPWORDS = {"a", "an", "and", "for", "the", "of", "to", "with", "our", "my", "in", "on", "new"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS}


def _line(prompt: str, label: str) -> str:
    m = re.search(rf"^{re.escape(label)}: (.*)$", prompt, re.MULTILINE)
    return m.group(1) if m else ""


def stub_llm(task: str, system: str, prompt: str) -> str:
    if task == "score_fit":
        job = _words(_line(prompt, "Job"))
        skills = _words(_line(prompt, "Freelancer listed skills"))
        overlap = len(job & skills)
        return '{"fit": %d}' % min(10, 3 + 2 * overlap)
    if task == "draft_outreach":
        name = _line(prompt, "Freelancer name") or "there"
        job = _line(prompt, "Job")
        return (f"Hi {name}, I'm reaching out on behalf of a client who needs: {job}. "
                "Could you reply with one total price in HKD for the whole job?")
    raise ValueError(f"stub has no answer for task {task!r}")
