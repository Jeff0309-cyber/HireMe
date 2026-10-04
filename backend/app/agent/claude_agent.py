"""The hiring agent on Claude: a manual tool-use loop over the shared toolbox (agent/toolbox.py).

We use a manual loop (not the SDK tool runner) so that every model turn and tool call is written
to the signed chain log, cached on disk for offline replay, and so the loop stops the moment a
hire is proposed. Thinking blocks are passed back unchanged, as the API requires.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.agent.toolbox import TOOL_SPECS, HiringToolbox, public_args, summary, system_prompt
from app.llm import LLMError

if TYPE_CHECKING:
    from app.agent.hiring import HiringSession

MAX_TURNS = 12
TOOLS = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"], "strict": True}
         for t in TOOL_SPECS]


class ClaudeHiringAgent:
    def __init__(self, session: "HiringSession"):
        self.s = session
        self.box = HiringToolbox(session)

    def _activity(self, kind: str, **data: Any) -> None:
        self.s.activity.append({"kind": kind, **data})

    def run(self) -> str:
        s = self.s
        model = s.llm.model_for_agent()
        base = {"model": model, "max_tokens": 16000, "system": system_prompt(s), "tools": TOOLS,
                "output_config": {"effort": "medium"}}
        messages: list[dict[str, Any]] = [{"role": "user", "content": "Find a freelancer for my task."}]
        self._activity("status", text=f"Claude ({model}) is working on it…")
        for _ in range(MAX_TURNS):
            try:
                response = s.llm.create_message("hiring_agent", {**base, "messages": messages})
            except LLMError as exc:
                self._activity("error", text=f"The AI agent stopped: {exc}")
                s.log.append("agent_error", {"error": str(exc)[:200]})
                return "error"
            content = response.get("content", [])
            messages.append({"role": "assistant", "content": content})     # passed back unchanged (incl. thinking)
            for block in content:
                if block.get("type") == "text" and block.get("text", "").strip():
                    text = block["text"].strip()
                    self._activity("agent", text=text)
                    s.log.append("agent_note", {"text": text[:1000], "source": "model"})
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                name, args = tu["name"], tu.get("input") or {}
                self._activity("tool", name=name, input=public_args(args))
                result = self.box.call(name, args)
                self._activity("result", name=name, ok="error" not in result, summary=summary(name, result))
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": json.dumps(result),
                                **({"is_error": True} if "error" in result else {})})
            messages.append({"role": "user", "content": results})
            if self.box.done:
                break
        if not self.box.done:
            s.no_match_reason = s.no_match_reason or "The agent stopped without proposing anyone."
            return "no_hire"
        return self.box.outcome or "no_hire"
