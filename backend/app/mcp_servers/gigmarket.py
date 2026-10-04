"""GigMarket MCP server: the SIMULATED Fiverr-style marketplace, exposed over the Model Context Protocol.

Why MCP: the agent doesn't need custom code per marketplace. Any marketplace that offers an MCP
server with these tools can be plugged in the same way; GigMarket is our stand-in until real
platforms offer one.

Run it on its own:    python -m app.mcp_servers.gigmarket          (stdio transport)

Tools
  list_categories()                       the categories on offer
  search_gigs(category)                   every gig in a category: seller stats, packages, listing text
  get_gig(gig_id)                         one gig
  send_message(gig_id, package, text)     message a seller; returns their (scripted) reply
  get_delivery(gig_id, words)             the seller's (simulated) delivered file

Everything returned is labelled SIMULATED. Sellers' hidden personas are never exposed.
There are deliberately no payment tools here: payment is a separate concern.
"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.marketplace.catalog import (CATEGORIES, PLATFORM, PLATFORM_NAME, Gig, catalog, delivery_message, gig,
                                     reply_to_inquiry, search, simulated_deliverable)

server = MCPServer(
    name="gigmarket",
    title=f"{PLATFORM_NAME} (SIMULATED)",
    instructions="A simulated freelance marketplace for the HireMe demo. All sellers, prices and messages are made up.",
    version="1.0.0",
)


def listing(g: Gig) -> dict[str, Any]:
    """Public view of a gig. Never includes the seller's hidden persona."""
    return {
        "gig_id": g.gig_id, "platform": PLATFORM, "category": g.category, "title": g.title,
        "description": g.description, "tags": list(g.tags), "reviews": g.reviews,
        "seller": g.seller.model_dump(mode="json"),
        "packages": [{"name": p.name, "price_minor": p.price_minor, "currency": "HKD", "delivery_days": p.delivery_days,
                      "revisions": p.revisions, "includes": p.includes} for p in g.packages],
        "label": "SIMULATED",
    }


def _find(gig_id: str) -> Gig:
    if gig_id not in {g.gig_id for g in catalog()}:
        raise ToolError(f"unknown gig {gig_id!r}")
    return gig(gig_id)


@server.tool(description="List the categories on GigMarket.")
def list_categories() -> dict:
    return {"categories": [{"id": c.id, "label": c.label} for c in CATEGORIES.values()], "label": "SIMULATED"}


@server.tool(description="Every gig in a category, with seller stats, packages (prices in HKD cents) and listing text.")
def search_gigs(category: str) -> dict:
    if category not in CATEGORIES:
        raise ToolError(f"unknown category {category!r}")
    return {"gigs": [listing(g) for g in search(category)], "label": "SIMULATED"}


@server.tool(description="One gig by id.")
def get_gig(gig_id: str) -> dict:
    return listing(_find(gig_id))


@server.tool(description="Send a message to a gig's seller about a package. Returns the seller's reply.")
def send_message(gig_id: str, package: str, text: str) -> dict:
    g = _find(gig_id)
    if package not in ("Basic", "Standard", "Premium"):
        raise ToolError("package must be Basic, Standard or Premium")
    return {"gig_id": gig_id, "reply": reply_to_inquiry(g, package), "label": "SIMULATED"}


@server.tool(description="The seller's delivered file for an order (simulated).")
def get_delivery(gig_id: str, words: int = 200) -> dict:
    g = _find(gig_id)
    filename, content = simulated_deliverable(g, max(1, min(int(words), 20_000)))
    return {"gig_id": gig_id, "filename": filename, "content": content, "message": delivery_message(g, filename),
            "label": "SIMULATED"}


if __name__ == "__main__":
    server.run("stdio")
