"""Gateway Manager for multi-gateway routing and upstream MCP aggregation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from arcade_core.catalog import ToolCatalog
from arcade_core.schema import ToolDefinition
from pydantic import BaseModel, Field

from arcade_mcp_server.gateway.upstream_client import (
    call_upstream_tool,
    fetch_upstream_tools,
    test_upstream_mcp_connection,
)

logger = logging.getLogger(__name__)


class UpstreamMCPServer(BaseModel):
    """Configuration for an external/upstream MCP server."""
    id: str = Field(..., description="Unique identifier for this upstream server")
    name: str = Field(..., description="Human-readable name")
    url: str = Field(..., description="Streamable HTTP or SSE URL of the upstream MCP server")
    transport: str = Field(default="streamable-http", description="Transport type (streamable-http or sse)")
    headers: dict[str, str] = Field(default_factory=dict, description="Custom headers with optional ${ENV_VAR} substitution")
    prefix: str = Field(default="", description="Optional namespace prefix for tool names (e.g. 'gh' -> 'gh_create_issue')")
    enabled: bool = Field(default=True, description="Whether this upstream server is enabled")


class GatewayConfig(BaseModel):
    """Configuration for a named MCP Gateway."""
    slug: str = Field(..., description="URL-safe slug for the gateway (e.g. 'ecommerce-app')")
    name: str = Field(..., description="Display name for the gateway")
    description: str = Field(default="", description="Description of the gateway purpose")
    enabled: bool = Field(default=True, description="Whether this gateway is active")
    included_local_tools: list[str] = Field(
        default_factory=lambda: ["*"],
        description="List of local tool names to include, or ['*'] for all local tools",
    )
    upstream_servers: list[UpstreamMCPServer] = Field(
        default_factory=list,
        description="List of upstream external MCP servers to aggregate",
    )
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class GatewayManager:
    """Manages creation, storage, and tool aggregation for multi-tenant MCP gateways."""

    def __init__(self, storage_path: Path | None = None) -> None:
        if storage_path is None:
            env_path = os.environ.get("GATEWAYS_CONFIG_PATH")
            if env_path:
                self._storage_path = Path(env_path)
            else:
                self._storage_path = Path.cwd() / "gateways" / "gateways.json"
        else:
            self._storage_path = storage_path

        self._gateways: dict[str, GatewayConfig] = {}
        self._load_from_storage()

    def _load_from_storage(self) -> None:
        """Load gateways from disk."""
        if not self._storage_path.exists():
            # Create default gateway if file doesn't exist
            default_gateway = GatewayConfig(
                slug="default",
                name="Default Gateway",
                description="Default gateway hosting all local tools and custom upstream MCPs",
                included_local_tools=["*"],
                upstream_servers=[],
            )
            self._gateways = {"default": default_gateway}
            self._save_to_storage()
            return

        try:
            content = self._storage_path.read_text(encoding="utf-8")
            data = json.loads(content)
            self._gateways = {}
            for item in data.get("gateways", []):
                gw = GatewayConfig.model_validate(item)
                self._gateways[gw.slug] = gw
        except Exception as exc:
            logger.exception(f"Failed to load gateways configuration from {self._storage_path}: {exc}")
            self._gateways = {}

    def _save_to_storage(self) -> None:
        """Persist gateways to disk."""
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": "1.0",
                "gateways": [gw.model_dump() for gw in self._gateways.values()],
            }
            self._storage_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.exception(f"Failed to save gateways to {self._storage_path}: {exc}")

    def list_gateways(self) -> list[GatewayConfig]:
        """List all configured gateways."""
        return list(self._gateways.values())

    def get_gateway(self, slug: str) -> GatewayConfig | None:
        """Get a gateway by slug."""
        return self._gateways.get(slug.lower().strip())

    def create_gateway(self, config: GatewayConfig) -> GatewayConfig:
        """Create and store a new gateway."""
        slug = config.slug.lower().strip()
        if not re.match(r"^[a-z0-9_\-]+$", slug):
            raise ValueError("Slug must contain only lowercase letters, numbers, hyphens, and underscores.")

        if slug in self._gateways:
            raise ValueError(f"Gateway with slug '{slug}' already exists.")

        config.slug = slug
        config.created_at = time.time()
        config.updated_at = time.time()
        self._gateways[slug] = config
        self._save_to_storage()
        return config

    def update_gateway(self, slug: str, config: GatewayConfig) -> GatewayConfig:
        """Update an existing gateway."""
        slug = slug.lower().strip()
        if slug not in self._gateways:
            raise KeyError(f"Gateway '{slug}' not found.")

        config.slug = slug
        config.updated_at = time.time()
        self._gateways[slug] = config
        self._save_to_storage()
        return config

    def delete_gateway(self, slug: str) -> bool:
        """Delete a gateway by slug."""
        slug = slug.lower().strip()
        if slug in self._gateways:
            del self._gateways[slug]
            self._save_to_storage()
            return True
        return False

    async def get_aggregated_tools(
        self,
        gateway: GatewayConfig,
        local_catalog: ToolCatalog,
    ) -> list[dict[str, Any]]:
        """Aggregate local tools and upstream MCP tools for a specific gateway."""
        tools: list[dict[str, Any]] = []

        # 1. Filter local tools
        include_all = "*" in gateway.included_local_tools
        included_set = set(gateway.included_local_tools)

        for t in local_catalog:
            tool_def = t.definition if hasattr(t, "definition") else t
            fqn = getattr(tool_def, "fully_qualified_name", tool_def.name)
            mcp_name = fqn.replace("::", "_").replace(".", "_")

            if include_all or tool_def.name in included_set or mcp_name in included_set or fqn in included_set:
                # Convert parameters to JSON Schema inputSchema
                properties: dict[str, Any] = {}
                required_props: list[str] = []

                tool_input = getattr(tool_def, "input", None) or getattr(tool_def, "inputs", None)
                if tool_input and hasattr(tool_input, "parameters") and tool_input.parameters:
                    for param in tool_input.parameters:
                        param_schema = getattr(param, "value_schema", None)
                        val_type = getattr(param_schema, "val_type", "string") if param_schema else "string"
                        # Map type names
                        if val_type in ("integer", "int"):
                            json_type = "integer"
                        elif val_type in ("float", "number"):
                            json_type = "number"
                        elif val_type == "boolean":
                            json_type = "boolean"
                        elif val_type in ("dict", "json", "object"):
                            json_type = "object"
                        elif val_type in ("list", "array"):
                            json_type = "array"
                        else:
                            json_type = "string"

                        properties[param.name] = {
                            "type": json_type,
                            "description": param.description or "",
                        }
                        if getattr(param, "required", True):
                            required_props.append(param.name)

                tools.append({
                    "name": mcp_name,
                    "local_name": tool_def.name,
                    "description": tool_def.description or "",
                    "inputSchema": {
                        "type": "object",
                        "properties": properties,
                        "required": required_props,
                    },
                    "is_upstream": False,
                    "origin": "Local Arcade Tools",
                })

        # 2. Fetch tools from active upstream MCP servers concurrently
        upstream_tasks = []
        for upstream in gateway.upstream_servers:
            if upstream.enabled and upstream.url:
                upstream_tasks.append(
                    fetch_upstream_tools(
                        url=upstream.url,
                        headers=upstream.headers,
                        prefix=upstream.prefix,
                    )
                )

        if upstream_tasks:
            results = await asyncio.gather(*upstream_tasks, return_exceptions=True)
            for i, res in enumerate(results):
                if isinstance(res, list):
                    upstream_name = gateway.upstream_servers[i].name
                    for ut in res:
                        ut["origin"] = f"Upstream: {upstream_name}"
                        tools.append(ut)
                else:
                    logger.warning(f"Failed to fetch tools from upstream: {res}")

        return tools

    async def execute_tool(
        self,
        gateway: GatewayConfig,
        local_catalog: ToolCatalog,
        tool_name: str,
        arguments: dict[str, Any],
        get_mcp_server: Callable[[], Any | None],
    ) -> dict[str, Any]:
        """Execute a tool within this gateway, either locally or by forwarding upstream."""
        # 1. Check if tool is in an upstream server
        for upstream in gateway.upstream_servers:
            if not upstream.enabled:
                continue

            # If tool name matches with or without prefix
            orig_name = tool_name
            if upstream.prefix and tool_name.startswith(f"{upstream.prefix}_"):
                orig_name = tool_name[len(upstream.prefix) + 1 :]

            # Try forwarding to upstream
            # First check if the tool is an upstream tool
            upstream_tools = await fetch_upstream_tools(upstream.url, headers=upstream.headers, prefix=upstream.prefix)
            matched = any(t["name"] == tool_name or t.get("original_name") == orig_name for t in upstream_tools)
            if matched:
                return await call_upstream_tool(
                    url=upstream.url,
                    tool_name=orig_name,
                    arguments=arguments,
                    headers=upstream.headers,
                )

        # 2. Check local tools
        target_tool = None
        for t in local_catalog:
            tool_def = t.definition if hasattr(t, "definition") else t
            fqn = getattr(tool_def, "fully_qualified_name", tool_def.name)
            mcp_name = fqn.replace("::", "_").replace(".", "_")
            if tool_def.name == tool_name or mcp_name == tool_name or fqn == tool_name:
                target_tool = t
                break

        if target_tool is not None:
            # Execute local tool
            func = getattr(target_tool, "tool", None) or getattr(target_tool, "func", None)
            if func is None and hasattr(target_tool, "definition"):
                func = getattr(target_tool.definition, "func", None)

            if func is None:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Callable for local tool '{tool_name}' not found."}],
                }

            import inspect
            from arcade_mcp_server.context import Context
            from arcade_core.schema import ToolContext

            mcp_server = get_mcp_server()
            sig = inspect.signature(func)
            kwargs_to_pass = dict(arguments)

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

            try:
                if inspect.iscoroutinefunction(func):
                    res = await func(**kwargs_to_pass)
                else:
                    loop = asyncio.get_event_loop()
                    res = await loop.run_in_executor(None, lambda: func(**kwargs_to_pass))

                text_out = json.dumps(res, default=str) if isinstance(res, (dict, list)) else str(res)
                return {
                    "isError": False,
                    "content": [{"type": "text", "text": text_out}],
                }
            except Exception as exc:
                logger.exception(f"Error executing local tool {tool_name}")
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Execution error: {exc}"}],
                }

        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Tool '{tool_name}' not found in gateway '{gateway.slug}'."}],
        }
