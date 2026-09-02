"""FastAPI router providing MCP protocol endpoints for named Gateways."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from arcade_core.catalog import ToolCatalog
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from arcade_mcp_server.gateway.manager import GatewayManager

logger = logging.getLogger(__name__)


def create_gateway_router(
    gateway_manager: GatewayManager,
    local_catalog: ToolCatalog,
    get_mcp_server: Callable[[], Any | None],
) -> APIRouter:
    """Create FastAPI router for multi-gateway MCP protocol handling."""
    router = APIRouter(prefix="/gateways", tags=["MCP Gateways"])

    @router.post("/{gateway_slug}/mcp")
    @router.post("/{gateway_slug}/mcp/")
    async def handle_gateway_mcp_jsonrpc(
        gateway_slug: str,
        request: Request,
    ) -> Response:
        """Handle MCP JSON-RPC 2.0 requests for a specific Gateway."""
        gateway = gateway_manager.get_gateway(gateway_slug)
        if gateway is None:
            return JSONResponse(
                status_code=404,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32600, "message": f"Gateway '{gateway_slug}' not found."},
                    "id": None,
                },
            )

        if not gateway.enabled:
            return JSONResponse(
                status_code=403,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32600, "message": f"Gateway '{gateway_slug}' is disabled."},
                    "id": None,
                },
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32700, "message": "Parse error: Invalid JSON payload"},
                    "id": None,
                },
            )

        method = body.get("method")
        req_id = body.get("id")
        params = body.get("params", {})

        # 1. MCP Initialize
        if method == "initialize":
            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "tools": {
                                "listChanged": False,
                            },
                        },
                        "serverInfo": {
                            "name": f"ArcadeGateway-{gateway.name}",
                            "version": "1.0.0",
                        },
                    },
                }
            )

        # 2. MCP Initialized notification
        if method == "notifications/initialized":
            return Response(status_code=204)

        # 3. Ping
        if method == "ping":
            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {},
                }
            )

        # 4. Tools / List
        if method == "tools/list":
            tools = await gateway_manager.get_aggregated_tools(gateway, local_catalog)
            # Standardize tools for MCP response
            formatted_tools = []
            for t in tools:
                formatted_tools.append({
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "inputSchema": t.get("inputSchema", {"type": "object", "properties": {}}),
                })

            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": formatted_tools,
                    },
                }
            )

        # 5. Tools / Call
        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})

            if not tool_name:
                return JSONResponse(
                    content={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": "Missing 'name' in tools/call parameters"},
                    }
                )

            result = await gateway_manager.execute_tool(
                gateway=gateway,
                local_catalog=local_catalog,
                tool_name=tool_name,
                arguments=arguments,
                get_mcp_server=get_mcp_server,
            )

            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": result,
                }
            )

        # Unknown method
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method '{method}' not supported."},
            }
        )

    return router
