#!/usr/bin/env python3
"""
Toolathlon Tool Server

A FastAPI server that exposes MCP tools via HTTP REST API.
This allows any agent framework to use Toolathlon's MCP tools.

Endpoints:
- GET  /health              - Health check
- GET  /tools               - List all available tools with schemas
- POST /tools/{tool_name}   - Execute a tool call
- GET  /servers             - List connected MCP servers
"""

import asyncio
import json
import os
import sys
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

# Add parent paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from utils.mcp.tool_servers import MCPServerManager, ToolCallError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ToolCallRequest(BaseModel):
    """Request body for tool execution."""
    arguments: Dict[str, Any] = {}


class ToolDefinition(BaseModel):
    """Tool definition in OpenAI function calling format."""
    name: str
    description: str
    parameters: Dict[str, Any]
    server: str  # Which MCP server provides this tool


class ToolCallResponse(BaseModel):
    """Response from tool execution."""
    success: bool
    result: Any = None
    error: Optional[str] = None


# Global MCP manager
mcp_manager: Optional[MCPServerManager] = None


def load_credentials_from_env() -> Dict[str, Any]:
    """Load credentials from environment variables."""
    credentials = {}

    # Map environment variables to token keys
    env_mapping = {
        # Google
        "GOOGLE_CLIENT_ID": "google_client_id",
        "GOOGLE_CLIENT_SECRET": "google_client_secret",
        "GOOGLE_REFRESH_TOKEN": "google_refresh_token",
        "GCP_PROJECT_ID": "gcp_project_id",
        "GCP_SERVICE_ACCOUNT_PATH": "gcp_service_account_path",
        "GOOGLE_CLOUD_API_KEY": "google_cloud_console_api_key",

        # GitHub
        "GITHUB_TOKEN": "github_token",
        "GITHUB_ALLOWED_REPOS": "github_allowed_repos",

        # Canvas
        "CANVAS_API_TOKEN": "canvas_api_token",
        "CANVAS_DOMAIN": "canvas_domain",

        # Notion
        "NOTION_INTEGRATION_KEY": "notion_integration_key",

        # Snowflake
        "SNOWFLAKE_ACCOUNT": "snowflake_account",
        "SNOWFLAKE_USER": "snowflake_user",
        "SNOWFLAKE_WAREHOUSE": "snowflake_warehouse",
        "SNOWFLAKE_ROLE": "snowflake_role",
        "SNOWFLAKE_PRIVATE_KEY_PATH": "snowflake_private_key_path",

        # WandB
        "WANDB_API_KEY": "wandb_api_key",

        # Huggingface
        "HUGGINGFACE_TOKEN": "huggingface_token",

        # Email
        "EMAILS_CONFIG_FILE": "emails_config_file",

        # WooCommerce
        "WOOCOMMERCE_API_KEY": "woocommerce_api_key",
        "WOOCOMMERCE_API_SECRET": "woocommerce_api_secret",
        "WOOCOMMERCE_SITE_URL": "woocommerce_site_url",

        # Serper (web search)
        "SERPER_API_KEY": "serper_api_key",

        # Google Cloud specific
        "GOOGLE_CLOUD_ALLOWED_BUCKETS": "google_cloud_allowed_buckets",
        "GOOGLE_CLOUD_ALLOWED_BIGQUERY_DATASETS": "google_cloud_allowed_bigquery_datasets",
        "GOOGLE_CLOUD_ALLOWED_LOG_BUCKETS": "google_cloud_allowed_log_buckets",
        "GOOGLE_CLOUD_ALLOWED_INSTANCES": "google_cloud_allowed_instances",

        # K8s
        "KUBECONFIG_PATH": "kubeconfig_path",
    }

    for env_var, token_key in env_mapping.items():
        value = os.environ.get(env_var)
        if value:
            credentials[token_key] = value

    return credentials


async def initialize_mcp_manager(
    servers_to_connect: List[str],
    agent_workspace: str,
    config_dir: str = "configs/mcp_servers",
    task_credentials: Optional[Dict[str, Any]] = None,
) -> MCPServerManager:
    """Initialize MCP server manager and connect to specified servers."""
    global mcp_manager

    # Load credentials from environment
    credentials = load_credentials_from_env()

    # Override with task-specific credentials
    if task_credentials:
        credentials.update(task_credentials)

    logger.info(f"Initializing MCP manager with workspace: {agent_workspace}")
    logger.info(f"Servers to connect: {servers_to_connect}")

    mcp_manager = MCPServerManager(
        agent_workspace=agent_workspace,
        config_dir=config_dir,
        debug=True,
        local_token_key_session=credentials,
    )

    # Connect to specified servers
    if servers_to_connect:
        await mcp_manager.connect_servers(servers_to_connect)
        logger.info(f"Connected to servers: {mcp_manager.get_connected_server_names()}")

    return mcp_manager


async def get_all_tools() -> List[ToolDefinition]:
    """Get all tools from connected MCP servers."""
    if not mcp_manager:
        return []

    tools = []
    for server in mcp_manager.get_all_connected_servers():
        try:
            server_tools = await server.list_tools()
            for tool in server_tools:
                # Convert to OpenAI function calling format
                schema = tool.inputSchema.copy() if tool.inputSchema else {}
                if "properties" not in schema:
                    schema["properties"] = {}
                if "type" not in schema:
                    schema["type"] = "object"

                tools.append(ToolDefinition(
                    name=f"{server.name}-{tool.name}",
                    description=tool.description or f"Tool {tool.name} from {server.name}",
                    parameters=schema,
                    server=server.name,
                ))
        except Exception as e:
            logger.error(f"Error listing tools from {server.name}: {e}")

    return tools


async def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> ToolCallResponse:
    """Execute a tool call."""
    if not mcp_manager:
        return ToolCallResponse(success=False, error="MCP manager not initialized")

    # Parse tool name: format is "server-tool_name"
    if "-" in tool_name:
        parts = tool_name.split("-", 1)
        server_name = parts[0]
        actual_tool_name = parts[1]
    else:
        return ToolCallResponse(success=False, error=f"Invalid tool name format: {tool_name}")

    # Find the server
    if server_name not in mcp_manager.connected_servers:
        return ToolCallResponse(
            success=False,
            error=f"Server '{server_name}' not connected. Available: {mcp_manager.get_connected_server_names()}"
        )

    server = mcp_manager.connected_servers[server_name]

    try:
        result = await server.call_tool(actual_tool_name, arguments)

        # Convert result to JSON-serializable format
        if hasattr(result, 'content'):
            if len(result.content) == 1:
                output = result.content[0].model_dump()
            else:
                output = [item.model_dump() for item in result.content]
        else:
            output = str(result)

        return ToolCallResponse(success=True, result=output)

    except ToolCallError as e:
        return ToolCallResponse(success=False, error=str(e))
    except Exception as e:
        logger.error(f"Error executing tool {tool_name}: {e}")
        return ToolCallResponse(success=False, error=str(e))


# FastAPI app
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    # Startup
    logger.info("Tool Server starting...")

    # Get configuration from environment
    servers = os.environ.get("TOOLATHLON_MCP_SERVERS", "").split(",")
    servers = [s.strip() for s in servers if s.strip()]

    workspace = os.environ.get("TOOLATHLON_WORKSPACE", "/app")
    config_dir = os.environ.get("TOOLATHLON_MCP_CONFIG_DIR", "configs/mcp_servers")

    # Load task-specific credentials if provided
    task_credentials_file = os.environ.get("TOOLATHLON_TASK_CREDENTIALS")
    task_credentials = None
    if task_credentials_file and os.path.exists(task_credentials_file):
        with open(task_credentials_file) as f:
            task_credentials = json.load(f)

    if servers:
        try:
            await initialize_mcp_manager(
                servers_to_connect=servers,
                agent_workspace=workspace,
                config_dir=config_dir,
                task_credentials=task_credentials,
            )
        except Exception as e:
            logger.error(f"Failed to initialize MCP manager: {e}")
    else:
        logger.warning("No MCP servers specified. Set TOOLATHLON_MCP_SERVERS env var.")

    yield

    # Shutdown
    logger.info("Tool Server shutting down...")
    if mcp_manager:
        await mcp_manager.ensure_all_disconnected()


app = FastAPI(
    title="Toolathlon Tool Server",
    description="HTTP API for Toolathlon MCP tools",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    connected = mcp_manager.get_connected_server_names() if mcp_manager else []
    return {
        "status": "healthy",
        "connected_servers": connected,
    }


@app.get("/servers")
async def list_servers():
    """List all connected MCP servers."""
    if not mcp_manager:
        return {"servers": [], "available": []}

    return {
        "connected": mcp_manager.get_connected_server_names(),
        "available": mcp_manager.get_available_servers(),
    }


@app.get("/tools")
async def list_tools():
    """
    List all available tools with their schemas.
    Returns tools in OpenAI function calling format.
    """
    tools = await get_all_tools()

    # Convert to OpenAI functions format
    functions = []
    for tool in tools:
        functions.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
            "metadata": {
                "server": tool.server,
            }
        })

    return {
        "tools": functions,
        "count": len(functions),
    }


@app.post("/tools/{tool_name}")
async def call_tool(tool_name: str, request: ToolCallRequest):
    """
    Execute a tool call.

    Args:
        tool_name: The tool name in format "server-tool_name"
        request: JSON body with "arguments" field

    Returns:
        Tool execution result
    """
    response = await execute_tool(tool_name, request.arguments)

    if not response.success:
        raise HTTPException(status_code=400, detail=response.error)

    return {
        "success": True,
        "result": response.result,
    }


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler."""
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": str(exc)},
    )


def main():
    """Run the tool server."""
    host = os.environ.get("TOOLATHLON_SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("TOOLATHLON_SERVER_PORT", "8000"))

    logger.info(f"Starting Tool Server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
