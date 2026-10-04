"""How the agent reaches a marketplace.

The hiring session only ever talks to a marketplace through this small interface:

    search(category) -> [GigListing]      get(gig_id) -> GigListing
    message(gig_id, package, text) -> reply text
    delivery(gig_id, words) -> (filename, content, seller message)

MCPMarketplace speaks the Model Context Protocol to an MCP server (by default our GigMarket server,
started as a subprocess over stdio). Any marketplace that ships an MCP server with these tools can
be plugged in without new agent code: that is the "on-ramp" MCP gives us. Every call is timed and
recorded so the UI and the signed log can show exactly what crossed the boundary.

LocalMarketplace calls the catalog directly. It is only for fast unit tests.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from app.contracts import Profile
from app.marketplace.catalog import PLATFORM, Package, catalog, delivery_message, gig, reply_to_inquiry, search, simulated_deliverable

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class GigListing:
    """A gig as the marketplace describes it. Listing text is UNTRUSTED."""
    gig_id: str
    platform: str
    category: str
    title: str
    description: str
    tags: tuple[str, ...]
    seller: Profile
    reviews: int
    packages: tuple[Package, ...]
    label: str = "SIMULATED"

    def package(self, name: str) -> Package:
        return next(p for p in self.packages if p.name == name)

    @staticmethod
    def from_json(d: dict) -> "GigListing":
        return GigListing(
            gig_id=d["gig_id"], platform=d.get("platform", PLATFORM), category=d["category"], title=d["title"],
            description=d.get("description", ""), tags=tuple(d.get("tags", [])), seller=Profile.model_validate(d["seller"]),
            reviews=int(d.get("reviews", 0)),
            packages=tuple(Package(p["name"], int(p["price_minor"]), int(p["delivery_days"]), int(p["revisions"]),
                                   p.get("includes", "")) for p in d["packages"]),
            label=d.get("label", "SIMULATED"))


class MarketplaceError(Exception):
    """The marketplace could not answer. Callers treat it as 'skip / no match', never as success."""


@dataclass
class CallRecord:
    server: str
    tool: str
    args: dict
    ms: int
    ok: bool
    summary: str


# --- local (tests) ------------------------------------------------------------------------------

class LocalMarketplace:
    name = "gigmarket (local)"
    transport = "in-process"

    def __init__(self) -> None:
        self.calls: list[CallRecord] = []

    def search(self, category: str) -> list[GigListing]:
        from app.mcp_servers.gigmarket import listing
        return [GigListing.from_json(listing(g)) for g in search(category)]

    def get(self, gig_id: str) -> GigListing:
        from app.mcp_servers.gigmarket import listing
        if gig_id not in {g.gig_id for g in catalog()}:
            raise MarketplaceError(f"unknown gig {gig_id}")
        return GigListing.from_json(listing(gig(gig_id)))

    def message(self, gig_id: str, package: str, text: str) -> str:
        return reply_to_inquiry(gig(gig_id), package)

    def delivery(self, gig_id: str, words: int) -> tuple[str, str, str]:
        g = gig(gig_id)
        filename, content = simulated_deliverable(g, words)
        return filename, content, delivery_message(g, filename)

    def status(self) -> dict:
        return {"name": self.name, "transport": self.transport, "connected": True, "tools": [], "calls": []}

    def close(self) -> None:
        pass


# --- MCP ------------------------------------------------------------------------------------------

def gigmarket_stdio_params():
    from mcp import StdioServerParameters
    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'backend'}{os.pathsep}{ROOT / 'verifier'}"}
    return StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_servers.gigmarket"], env=env, cwd=str(ROOT))


class MCPMarketplace:
    """A marketplace reached over MCP. Sync facade over the async MCP client, which runs on its own
    event loop in a background thread and keeps one connection open for the session."""

    def __init__(self, server: Any = None, name: str = "gigmarket", on_call: Optional[Callable[[CallRecord], None]] = None,
                 timeout_s: float = 30.0):
        self.name = name
        self._server = server if server is not None else gigmarket_stdio_params()
        self.transport = "stdio" if server is None or type(server).__name__ == "StdioServerParameters" else "in-process"
        self.on_call = on_call
        self.timeout_s = timeout_s
        self.calls: list[CallRecord] = []
        self.tools: list[dict] = []
        self.error: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None
        self._stop: Optional[asyncio.Event] = None
        self._lock = threading.Lock()

    # connection ---------------------------------------------------------------------------------
    def _ensure(self) -> None:
        with self._lock:
            if self._client is not None:
                return
            ready = threading.Event()
            box: dict[str, Any] = {}

            async def runner():
                from mcp import Client
                self._stop = asyncio.Event()
                try:
                    async with Client(self._server, read_timeout_seconds=self.timeout_s) as client:
                        listed = await client.list_tools()
                        self.tools = [{"name": t.name, "description": (t.description or "")[:140]} for t in listed.tools]
                        box["client"] = client
                        ready.set()
                        await self._stop.wait()
                except BaseException as exc:  # noqa: BLE001 - surfaced through box
                    box["error"] = exc
                    ready.set()

            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, daemon=True, name=f"mcp-{self.name}").start()
            asyncio.run_coroutine_threadsafe(runner(), self._loop)
            if not ready.wait(self.timeout_s):
                raise MarketplaceError("MCP server did not start in time")
            if "error" in box:
                self.error = f"{type(box['error']).__name__}: {box['error']}"[:200]
                raise MarketplaceError(f"could not connect to MCP server: {self.error}")
            self._client = box["client"]

    def close(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._client = None

    def _call(self, tool: str, args: dict) -> dict:
        self._ensure()
        t0 = time.perf_counter()
        ok, summary = False, ""
        try:
            fut = asyncio.run_coroutine_threadsafe(self._client.call_tool(tool, args), self._loop)
            result = fut.result(self.timeout_s)
            text = "".join(getattr(c, "text", "") for c in result.content)
            if result.is_error:
                summary = text[:160]
                raise MarketplaceError(summary)
            data = json.loads(text) if text else {}
            ok = True
            summary = _summarise(tool, data)
            return data
        except MarketplaceError:
            raise
        except Exception as exc:  # noqa: BLE001 - timeouts, transport errors, bad JSON
            summary = f"{type(exc).__name__}"
            raise MarketplaceError(f"MCP call {tool} failed: {summary}") from exc
        finally:
            rec = CallRecord(self.name, tool, {k: (v if len(str(v)) < 80 else str(v)[:77] + "...") for k, v in args.items()},
                             int((time.perf_counter() - t0) * 1000), ok, summary)
            self.calls.append(rec)
            if self.on_call:
                self.on_call(rec)

    # marketplace interface ---------------------------------------------------------------------------
    def search(self, category: str) -> list[GigListing]:
        return [GigListing.from_json(g) for g in self._call("search_gigs", {"category": category})["gigs"]]

    def get(self, gig_id: str) -> GigListing:
        return GigListing.from_json(self._call("get_gig", {"gig_id": gig_id}))

    def message(self, gig_id: str, package: str, text: str) -> str:
        return str(self._call("send_message", {"gig_id": gig_id, "package": package, "text": text})["reply"])

    def delivery(self, gig_id: str, words: int) -> tuple[str, str, str]:
        d = self._call("get_delivery", {"gig_id": gig_id, "words": words})
        return d["filename"], d["content"], d["message"]

    def status(self) -> dict:
        return {"name": self.name, "transport": self.transport, "connected": self._client is not None,
                "error": self.error, "tools": self.tools,
                "calls": [c.__dict__ for c in self.calls[-50:]]}


def _summarise(tool: str, data: dict) -> str:
    if tool == "search_gigs":
        return f"{len(data.get('gigs', []))} gigs"
    if tool == "send_message":
        return "seller replied"
    if tool == "get_delivery":
        return data.get("filename", "file")
    if tool == "get_gig":
        return data.get("title", "")[:60]
    return "ok"
