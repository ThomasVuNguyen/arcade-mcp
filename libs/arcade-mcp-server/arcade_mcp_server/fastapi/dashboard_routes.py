"""FastAPI router for the self-hosted Arcade MCP Dashboard & Tool Playground."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arcade_core.catalog import ToolCatalog
from arcade_core.schema import ToolContext, ToolDefinition
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from arcade_mcp_server.context import Context
from arcade_mcp_server.server import MCPServer
from arcade_mcp_server.settings import MCPSettings

logger = logging.getLogger(__name__)

# Track server start time
_SERVER_START_TIME = time.time()


class ToolExecuteRequest(BaseModel):
    name: str = Field(..., description="Tool name to execute")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Arguments to pass to the tool")


class ToolCreateRequest(BaseModel):
    filename: str = Field(..., description="Filename in tools/ (e.g. custom_tools.py)")
    code: str = Field(..., description="Python source code containing @tool definitions")


def _get_static_html_path() -> Path:
    """Get path to the dashboard HTML file."""
    return Path(__file__).parent.parent / "static" / "index.html"


def _extract_tool_def(tool_obj: Any) -> ToolDefinition:
    """Extract ToolDefinition from MaterializedTool or ToolDefinition."""
    if hasattr(tool_obj, "definition"):
        return tool_obj.definition
    return tool_obj


def _extract_tool_func(tool_obj: Any) -> Callable[..., Any] | None:
    """Extract the callable function from MaterializedTool or ToolDefinition."""
    if hasattr(tool_obj, "tool") and callable(tool_obj.tool):
        return tool_obj.tool
    if hasattr(tool_obj, "func") and callable(tool_obj.func):
        return tool_obj.func
    if hasattr(tool_obj, "definition") and hasattr(tool_obj.definition, "func"):
        return tool_obj.definition.func
    return None


def create_dashboard_router(
    catalog: ToolCatalog,
    mcp_settings: MCPSettings,
    get_mcp_server: Callable[[], MCPServer | None],
) -> APIRouter:
    """Create FastAPI router providing the dashboard UI and admin APIs."""
    router = APIRouter(tags=["Arcade Dashboard"])

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    @router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard_ui(request: Request) -> HTMLResponse:
        """Serve the Dashboard Single Page Application."""
        html_file = _get_static_html_path()
        if html_file.exists():
            return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
        return HTMLResponse(
            content="<h1>Arcade MCP Dashboard</h1><p>Dashboard UI template not found.</p>",
            status_code=500,
        )

    @router.get("/api/dashboard/overview")
    async def get_overview(request: Request) -> dict[str, Any]:
        """Get high-level server statistics and status."""
        uptime = int(time.time() - _SERVER_START_TIME)
        mcp_server = get_mcp_server()

        tools_count = len(catalog) if catalog else (len(mcp_server.tools) if mcp_server and hasattr(mcp_server, "tools") else 0)
        resources_count = 0
        if mcp_server and hasattr(mcp_server, "resources"):
            if hasattr(mcp_server.resources, "_resources"):
                resources_count = len(mcp_server.resources._resources)
            elif hasattr(mcp_server.resources, "resources"):
                resources_count = len(mcp_server.resources.resources)

        # Discover configured secrets
        all_env_keys = sorted([k for k in os.environ.keys() if not k.startswith("_") and not k.startswith("MCP_")])

        # Discover required secrets across all tools
        required_secrets_set: set[str] = set()
        for t in catalog:
            tool_def = _extract_tool_def(t)
            if hasattr(tool_def, "requirements") and tool_def.requirements and tool_def.requirements.secrets:
                for sec in tool_def.requirements.secrets:
                    required_secrets_set.add(sec.key if hasattr(sec, "key") else str(sec))

        missing_secrets = [s for s in required_secrets_set if s not in os.environ]

        # Determine public host/URL from request headers
        host_header = request.headers.get("host", "arcade.beenex.org")
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        base_url = f"{proto}://{host_header}"

        return {
            "server": {
                "name": mcp_settings.server.name,
                "version": mcp_settings.server.version,
                "title": mcp_settings.server.title or mcp_settings.server.name,
                "instructions": mcp_settings.server.instructions or "",
            },
            "status": "healthy",
            "uptime_seconds": uptime,
            "uptime_formatted": f"{uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s",
            "counts": {
                "tools": tools_count,
                "resources": resources_count,
                "configured_secrets": len(all_env_keys),
                "required_secrets": len(required_secrets_set),
                "missing_secrets": len(missing_secrets),
            },
            "endpoints": {
                "mcp": f"{base_url}/mcp/",
                "docs": f"{base_url}/docs",
                "openapi": f"{base_url}/openapi.json",
                "dashboard": f"{base_url}/dashboard",
            },
            "transport": "http",
        }

    @router.get("/api/dashboard/tools")
    async def get_tools() -> list[dict[str, Any]]:
        """List all tools with full schema, parameters, and requirements."""
        tools_list: list[dict[str, Any]] = []

        for t in catalog:
            tool_def = _extract_tool_def(t)

            # Build tool parameters list
            params: list[dict[str, Any]] = []
            tool_input = getattr(tool_def, "input", None) or getattr(tool_def, "inputs", None)
            if tool_input and hasattr(tool_input, "parameters") and tool_input.parameters:
                for param in tool_input.parameters:
                    param_val_schema = getattr(param, "value_schema", None)
                    val_type = getattr(param_val_schema, "val_type", "string") if param_val_schema else "string"
                    enum_vals = getattr(param_val_schema, "enums", None) or getattr(param_val_schema, "enum", None) if param_val_schema else None
                    inner_val_type = getattr(param_val_schema, "inner_val_type", None) if param_val_schema else None

                    params.append({
                        "name": param.name,
                        "description": param.description or "",
                        "type": val_type,
                        "inner_type": inner_val_type,
                        "required": getattr(param, "required", True),
                        "inferrable": getattr(param, "inferrable", True),
                        "enums": enum_vals,
                    })

            # Check secrets
            required_secrets: list[str] = []
            if hasattr(tool_def, "requirements") and tool_def.requirements and tool_def.requirements.secrets:
                for sec in tool_def.requirements.secrets:
                    key = sec.key if hasattr(sec, "key") else str(sec)
                    required_secrets.append(key)

            # Check auth
            auth_provider = None
            if hasattr(tool_def, "requirements") and tool_def.requirements and tool_def.requirements.authorization:
                auth_req = tool_def.requirements.authorization
                auth_provider = getattr(auth_req, "provider_id", None) or str(type(auth_req).__name__)

            # Standardized MCP name
            fqn = getattr(tool_def, "fully_qualified_name", tool_def.name)
            mcp_tool_name = fqn.replace("::", "_").replace(".", "_")

            toolkit_name = "Default"
            if hasattr(tool_def, "toolkit") and tool_def.toolkit:
                toolkit_name = getattr(tool_def.toolkit, "name", "Default")

            tools_list.append({
                "name": tool_def.name,
                "mcp_name": mcp_tool_name,
                "toolkit": toolkit_name,
                "description": tool_def.description or "No description provided.",
                "parameters": params,
                "required_secrets": required_secrets,
                "requires_auth": auth_provider,
                "has_all_secrets": all(s in os.environ for s in required_secrets),
            })

        return tools_list

    @router.post("/api/dashboard/tools/execute")
    async def execute_tool(payload: ToolExecuteRequest) -> dict[str, Any]:
        """Execute a tool directly from the dashboard playground."""
        tool_name = payload.name.strip()
        args = payload.arguments or {}

        # Look up tool in catalog
        target_tool = None
        for t in catalog:
            tool_def = _extract_tool_def(t)
            fqn = getattr(tool_def, "fully_qualified_name", tool_def.name)
            mcp_name = fqn.replace("::", "_").replace(".", "_")
            if tool_def.name == tool_name or mcp_name == tool_name or fqn == tool_name:
                target_tool = t
                break

        if target_tool is None:
            raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found in catalog.")

        tool_def = _extract_tool_def(target_tool)
        func = _extract_tool_func(target_tool)

        if func is None:
            raise HTTPException(status_code=500, detail=f"Callable function for tool '{tool_name}' not found.")

        # Check required secrets
        if hasattr(tool_def, "requirements") and tool_def.requirements and tool_def.requirements.secrets:
            missing = [s.key for s in tool_def.requirements.secrets if hasattr(s, "key") and s.key not in os.environ]
            if missing:
                return {
                    "success": False,
                    "error": f"Missing required environment variable(s): {', '.join(missing)}. Configure them in Coolify.",
                    "missing_secrets": missing,
                    "execution_time_ms": 0,
                }

        start_time = time.perf_counter()
        try:
            # Build mock context if required
            sig = inspect.signature(func)
            kwargs_to_pass = dict(args)

            # Check if func accepts Context
            mcp_server = get_mcp_server()
            for param_name, param in sig.parameters.items():
                if param.annotation in (Context, ToolContext) or param_name == "context":
                    if mcp_server is not None:
                        ctx = Context(server=mcp_server)
                    else:
                        class _DummyServer:
                            pass
                        ctx = Context(server=_DummyServer())
                    kwargs_to_pass[param_name] = ctx
                    break

            # Execute async or sync function
            if inspect.iscoroutinefunction(func):
                result = await func(**kwargs_to_pass)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: func(**kwargs_to_pass))

            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return {
                "success": True,
                "result": result,
                "execution_time_ms": elapsed_ms,
            }
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.exception(f"Error executing tool {tool_name}")
            return {
                "success": False,
                "error": str(exc),
                "execution_time_ms": elapsed_ms,
            }

    @router.get("/api/dashboard/secrets")
    async def get_secrets_status() -> dict[str, Any]:
        """List secrets status: required secrets across tools and all active tool secrets."""
        # Find all required secrets
        tool_secret_map: dict[str, list[str]] = {}
        for t in catalog:
            tool_def = _extract_tool_def(t)
            if hasattr(tool_def, "requirements") and tool_def.requirements and tool_def.requirements.secrets:
                for sec in tool_def.requirements.secrets:
                    key = sec.key if hasattr(sec, "key") else str(sec)
                    if key not in tool_secret_map:
                        tool_secret_map[key] = []
                    tool_secret_map[key].append(tool_def.name)

        required_list = []
        for key, tools in tool_secret_map.items():
            is_set = key in os.environ
            val = os.environ.get(key, "")
            preview = f"{val[:3]}...{val[-3:]}" if (is_set and len(val) >= 6) else ("••••••" if is_set else None)
            required_list.append({
                "key": key,
                "is_configured": is_set,
                "used_by_tools": tools,
                "preview": preview,
            })

        # All tool environment keys
        all_env_keys = sorted([k for k in os.environ.keys() if not k.startswith("_") and not k.startswith("MCP_")])
        other_keys = [
            {
                "key": k,
                "is_configured": True,
                "preview": f"{os.environ[k][:3]}...{os.environ[k][-3:]}" if len(os.environ[k]) >= 6 else "••••••",
            }
            for k in all_env_keys if k not in tool_secret_map
        ]

        return {
            "required_secrets": required_list,
            "other_configured_secrets": other_keys,
            "instructions": "To add or change secrets in Coolify: Navigate to Starship -> arcade-mcp -> Environment Variables, add your keys, and click Redeploy.",
        }

    @router.post("/api/dashboard/tools/create")
    async def create_tool_file(payload: ToolCreateRequest) -> dict[str, Any]:
        """Save a new Python tool file into the tools/ directory."""
        filename = payload.filename.strip()
        code = payload.code

        # Safety checks
        if not filename.endswith(".py"):
            filename = f"{filename}.py"
        clean_name = os.path.basename(filename)
        if not re.match(r"^[a-zA-Z0-9_\-]+\.py$", clean_name):
            raise HTTPException(status_code=400, detail="Invalid filename. Use alphanumeric characters and underscores only.")

        # Resolve path
        tools_dir = Path.cwd() / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        target_path = tools_dir / clean_name

        try:
            target_path.write_text(code, encoding="utf-8")
            logger.info(f"Created new tool file: {target_path}")
            return {
                "success": True,
                "message": f"Successfully created tools/{clean_name}. Redeploy or reload to load new tools.",
                "file_path": str(target_path),
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to write file: {exc}") from exc

    @router.get("/api/dashboard/config-snippets")
    async def get_config_snippets(request: Request) -> dict[str, Any]:
        """Generate client configuration snippets for Claude, Cursor, LibreChat, and SDKs."""
        host_header = request.headers.get("host", "arcade.beenex.org")
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        mcp_url = f"{proto}://{host_header}/mcp/"

        claude_config = {
            "mcpServers": {
                "arcade": {
                    "url": mcp_url,
                    "transport": "streamable-http",
                }
            }
        }

        cursor_config = {
            "mcpServers": {
                "arcade": {
                    "url": mcp_url,
                }
            }
        }

        librechat_config = f"""mcpServers:
  arcade:
    type: "streamable-http"
    url: "{mcp_url}"
"""

        python_snippet = f"""from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("{mcp_url}") as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("Available Tools:", [t.name for t in tools.tools])
"""

        return {
            "mcp_url": mcp_url,
            "claude_desktop": claude_config,
            "cursor": cursor_config,
            "librechat": librechat_config,
            "python_sdk": python_snippet,
        }

    return router
