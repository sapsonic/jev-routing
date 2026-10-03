"""MCP entry point for the local Jev skill router."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from jev_router import route_skill

mcp = FastMCP("jev_skill_router")


@mcp.tool(name="jev_route_skill")
def jev_route_skill(user_request: str, conversation_context: str = "") -> dict:
    """Use Jev to choose one installed Hermes skill from the installed skill registry."""
    try:
        return route_skill(user_request, conversation_context)
    except (RuntimeError, ValueError) as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
