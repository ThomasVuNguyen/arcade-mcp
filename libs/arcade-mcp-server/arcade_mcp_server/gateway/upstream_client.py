"""Client helper for interacting with upstream / external MCP servers."""

from __future__ import annotations

import logging
import os
import re
from typing import Any
import httpx

logger = logging.getLogger(__name__)


def expand_env_vars(headers: dict[str, str]) -> dict[str, str]:
    """Expand ${VAR_NAME} placeholders in HTTP headers using current environment variables."""
    expanded: dict[str, str] = {}
    for k, v in headers.items():
        if isinstance(v, str):
            def repl(match: re.Match[str]) -> str:
                var_name = match.group(1)
                return os.environ.get(var_name, "")
            expanded[k] = re.sub(r"\$\{([A-Za-z0-9_]+)\}", repl, v)
        else:
            expanded[k] = v
    return expanded


async def test_upstream_mcp_connection(
    url: str,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Test connection to an upstream MCP server by initializing and requesting tools/list."""
    clean_headers = expand_env_vars(headers or {})
    clean_headers.setdefault("Content-Type", "application/json")
    clean_headers.setdefault("Accept", "application/json, text/event-stream")

    # Step 1: Send MCP initialize request
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {
                "name": "ArcadeGatewayAggregator",
                "version": "1.0.0",
            },
        },
    }

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        try:
            # Try POST to the MCP endpoint
            resp = await client.post(url, json=init_payload, headers=clean_headers)
            if resp.status_code >= 400:
                return {
                    "success": False,
                    "error": f"Upstream server responded with HTTP {resp.status_code}: {resp.text[:300]}",
                }

            init_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}

            # Step 2: Request tools/list
            list_payload = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
            list_resp = await client.post(url, json=list_payload, headers=clean_headers)
            if list_resp.status_code < 400:
                list_data = list_resp.json()
                tools = list_data.get("result", {}).get("tools", [])
                return {
                    "success": True,
                    "server_info": init_data.get("result", {}).get("serverInfo", {}),
                    "tools_count": len(tools),
                    "tools": tools,
                }

            return {
                "success": True,
                "server_info": init_data.get("result", {}).get("serverInfo", {}),
                "tools_count": 0,
                "tools": [],
            }
        except Exception as exc:
            logger.warning(f"Failed to connect to upstream MCP at {url}: {exc}")
            return {
                "success": False,
                "error": str(exc),
            }


async def fetch_upstream_tools(
    url: str,
    headers: dict[str, str] | None = None,
    prefix: str = "",
    timeout_seconds: float = 10.0,
) -> list[dict[str, Any]]:
    """Fetch all tools exposed by an upstream MCP server, optionally prefixing names."""
    clean_headers = expand_env_vars(headers or {})
    clean_headers.setdefault("Content-Type", "application/json")
    clean_headers.setdefault("Accept", "application/json, text/event-stream")

    list_payload = {
        "jsonrpc": "2.0",
        "id": "list_tools",
        "method": "tools/list",
        "params": {},
    }

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        try:
            resp = await client.post(url, json=list_payload, headers=clean_headers)
            if resp.status_code >= 400:
                logger.warning(f"Upstream MCP {url} returned HTTP {resp.status_code}")
                return []

            data = resp.json()
            raw_tools = data.get("result", {}).get("tools", [])
            formatted_tools: list[dict[str, Any]] = []

            for t in raw_tools:
                orig_name = t.get("name", "")
                tool_name = f"{prefix}_{orig_name}" if prefix and not orig_name.startswith(f"{prefix}_") else orig_name
                formatted_tools.append({
                    "name": tool_name,
                    "original_name": orig_name,
                    "description": t.get("description", ""),
                    "inputSchema": t.get("inputSchema", {}),
                    "upstream_url": url,
                    "is_upstream": True,
                })
            return formatted_tools
        except Exception as exc:
            logger.warning(f"Error fetching tools from upstream {url}: {exc}")
            return []


async def call_upstream_tool(
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Forward a tools/call request to an upstream MCP server."""
    clean_headers = expand_env_vars(headers or {})
    clean_headers.setdefault("Content-Type", "application/json")
    clean_headers.setdefault("Accept", "application/json, text/event-stream")

    call_payload = {
        "jsonrpc": "2.0",
        "id": "call_tool",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        try:
            resp = await client.post(url, json=call_payload, headers=clean_headers)
            if resp.status_code >= 400:
                return {
                    "isError": True,
                    "content": [{
                        "type": "text",
                        "text": f"Upstream MCP returned HTTP {resp.status_code}: {resp.text[:500]}",
                    }],
                }

            data = resp.json()
            if "result" in data:
                return data["result"]
            if "error" in data:
                return {
                    "isError": True,
                    "content": [{
                        "type": "text",
                        "text": f"Upstream error: {data['error'].get('message', 'Unknown error')}",
                    }],
                }
            return {
                "isError": False,
                "content": [{"type": "text", "text": str(data)}],
            }
        except Exception as exc:
            logger.exception(f"Error calling upstream tool {tool_name} at {url}")
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"Failed to reach upstream MCP: {exc}"}],
            }
