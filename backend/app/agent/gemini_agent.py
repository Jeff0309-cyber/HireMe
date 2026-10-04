"""The hiring agent on Gemini: a function-calling loop over the shared toolbox (agent/toolbox.py).

Each turn: send the conversation + tool declarations to Gemini; show its thought summary and text
in the UI; run every functionCall through the toolbox (which reaches the marketplace over MCP);
send back functionResponse parts; repeat until a hire is proposed or the agent finishes.
The model's content (including thought signatures) is appended back unchanged, as Gemini requires.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.agent.toolbox import TOOL_SPECS, HiringToolbox, public_args, summary, system_prompt
from app.llm import LLMError

if TYPE_CHECKING:
    from app.agent.hiring import HiringSession

MAX_TURNS = 12
TOOLS = [{"functionDeclarations": [{"name": t["name"], "description": t["description"],
                                    "parametersJsonSchema": t["schema"]} for t in TOOL_SPECS]}]


class GeminiHiringAgent:
    def __init__(self, session: "HiringSession"):
        self.s = session
        self.box = HiringToolbox(session)

    def _activity(self, kind: str, **data: Any) -> None:
        self.s.activity.append({"kind": kind, **data})

    def run(self) -> str:
        s = self.s
        model = s.llm.model_for_agent()
        contents: list[dict[str, Any]] = [{"role": "user", "parts": [{"text": "Find a freelancer for my task."}]}]
        self._activity("status", text=f"Gemini ({model}) is working on it…")
        for _ in range(MAX_TURNS):
            body = {"systemInstruction": {"parts": [{"text": system_prompt(s)}]}, "contents": contents, "tools": TOOLS,
                    "generationConfig": {"thinkingConfig": {"includeThoughts": True}}}
            try:
                response = s.llm.gemini_generate("hiring_agent", model, body)
            except LLMError as exc:
                self._activity("error", text=f"The AI agent stopped: {exc}")
                s.log.append("agent_error", {"error": str(exc)[:200]})
                return "error"
            content = response["candidates"][0]["content"]
            contents.append(content)                                   # unchanged, incl. thought signatures
            calls = []
            for part in content.get("parts", []):
                if part.get("thought") and part.get("text", "").strip():
                    self._activity("thinking", text=part["text"].strip())
                elif part.get("text", "").strip():
                    text = part["text"].strip()
                    self._activity("agent", text=text)
                    s.log.append("agent_note", {"text": text[:1000], "source": "model"})
                elif "functionCall" in part:
                    calls.append(part["functionCall"])
            if not calls:
                break
            responses = []
            for fc in calls:
                name, args = fc.get("name", ""), fc.get("args") or {}
                self._activity("tool", name=name, input=public_args(args))
                result = self.box.call(name, args)
                self._activity("result", name=name, ok="error" not in result, summary=summary(name, result))
                fr = {"name": name, "response": result}
                if fc.get("id"):
                    fr["id"] = fc["id"]
                responses.append({"functionResponse": fr})
            contents.append({"role": "user", "parts": responses})
            if self.box.done:
                break
        if not self.box.done:
            s.no_match_reason = s.no_match_reason or "The agent stopped without proposing anyone."
            return "no_hire"
        return self.box.outcome or "no_hire"
