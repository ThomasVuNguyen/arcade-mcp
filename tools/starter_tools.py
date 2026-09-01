"""Starter MCP tools for Arcade MCP Server."""

from typing import Annotated
from arcade_tdk import tool
from arcade_mcp_server import Context


@tool
def ping() -> str:
    """Check server health and connectivity."""
    return "pong"


@tool
def echo(message: Annotated[str, "Message to echo back"]) -> str:
    """Echo back the provided message."""
    return f"Echo: {message}"


@tool
def system_info(context: Context) -> dict:
    """Get basic server and context information."""
    return {
        "status": "healthy",
        "server": "arcade-mcp",
        "user_id": context.user_id if hasattr(context, "user_id") else None,
    }
