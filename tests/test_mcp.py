"""GigMarket over MCP: the server's tools, the client facade, and the hiring flow through it."""
import json

import pytest

from app.agent.hiring import HiringSession, RegisterForm
from app.ledger.keys import demo_key
from app.marketplace.access import MCPMarketplace, MarketplaceError
from app.mcp_servers.gigmarket import server
from test_hiring import LOGO, clock_from
from verify_log import verify_lines


@pytest.fixture
def market():
    m = MCPMarketplace(server=server)          # real MCP protocol, server in-process
    yield m
    m.close()


def test_tools_and_listing_shape(market):
    gigs = market.search("logo_design")
    assert len(gigs) == 6 and {t["name"] for t in market.tools} == {
        "list_categories", "search_gigs", "get_gig", "send_message", "get_delivery"}
    g = gigs[0]
    assert g.label == "SIMULATED" and g.seller.untrusted and g.packages[0].price_minor > 0
    assert market.get(g.gig_id).title == g.title


def test_personas_are_never_exposed(market):
    raw = json.dumps([g.__dict__ for g in market.search("logo_design")], default=str)
    assert "persona" not in raw and "upseller" not in raw and "injection" not in raw


def test_errors_become_marketplace_errors(market):
    with pytest.raises(MarketplaceError):
        market.get("nope")
    with pytest.raises(MarketplaceError):
        market.search("crypto")
    assert market.calls[-1].ok is False


def test_hiring_flow_runs_over_mcp_and_logs_every_call(market):
    s = HiringSession(clock_from(), demo_key("user"), demo_key("agent"), market=market)
    b = s.draft(LOGO)
    s.register(RegisterForm(title=b.title, category=b.category, budget_hkd="2000", deadline_days=7))
    s.search()
    s.confirm()
    s.deliver()
    s.release()
    assert s.stage == "released"
    entries = s.log.entries()
    mcp = [e["inputs"] for e in entries if e["event_type"] == "mcp_call"]
    tools = [c["tool"] for c in mcp]
    assert tools[0] == "search_gigs" and "send_message" in tools and "get_delivery" in tools
    assert all(c["server"] == "gigmarket" and c["ok"] for c in mcp)
    assert any(a["kind"] == "mcp" for a in s.activity)
    snap = s.snapshot()["connections"][0]
    assert snap["connected"] and snap["transport"] == "in-process" and snap["calls"]
    assert verify_lines([json.dumps(e) for e in entries], s.log.public_key_hex, expect_head=s.log.head_hash).ok


def test_marketplace_down_means_no_match_and_nothing_paid():
    class Down(MCPMarketplace):
        def search(self, category):
            raise MarketplaceError("connection refused")
    m = Down(server=server)
    s = HiringSession(clock_from(), demo_key("user"), demo_key("agent"), market=m)
    b = s.draft(LOGO)
    s.register(RegisterForm(title=b.title, category=b.category, budget_hkd="2000", deadline_days=7))
    s.search()
    assert s.stage == "no_match" and "marketplace" in s.no_match_reason and s.escrow.ledger == []
    m.close()


def test_stdio_subprocess_server():
    m = MCPMarketplace()                       # the real thing: server as a subprocess over stdio
    try:
        assert m.transport == "stdio"
        assert "HKD" in m.message("g01", "Basic", "Hello, total price in HKD?")
    finally:
        m.close()
