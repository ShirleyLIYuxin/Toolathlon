# Toolathlon Harbor Integration

This adapter enables running Toolathlon tasks through the Harbor evaluation framework.

## Important: Not Self-Contained

This adapter is **NOT** self-contained. It requires the full Toolathlon repository because:

- **Tool Server** imports `MCPServerManager` from `utils/mcp/tool_servers.py`
- **MCP Configs** are loaded from `configs/mcp_servers/*.yaml`
- **Credentials** come from `configs/token_key_session.py`
- **Docker Image** copies the entire Toolathlon workspace

Run all setup from the **Toolathlon root directory**, not this adapter folder.

## Quick Start

```bash
# From Toolathlon root directory
cd /path/to/Toolathlon

# Run interactive setup (credentials, Docker, task conversion)
bash adapters/toolathlon/scripts/setup.sh

# Run with Harbor
harbor run \
    --dataset-path adapters/toolathlon/harbor_tasks \
    --agent claude-code \
    --model anthropic/claude-sonnet-4
```

## Full Setup Guide

See [docs/SETUP.md](docs/SETUP.md) for detailed instructions including:
- Google Cloud setup (automated)
- GitHub/HuggingFace/WandB/Serper setup (automated)
- Notion setup (manual)
- Snowflake setup (manual)
- Local services deployment

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Harbor Docker Container                         │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │                         ENVIRONMENT                             │ │
│  │                                                                 │ │
│  │   ┌─────────────┐          ┌─────────────────────────────────┐ │ │
│  │   │ Tool Server │ ◄─────── │        MCP Servers              │ │ │
│  │   │ (HTTP API)  │   MCP    │  ┌───────┐ ┌────────┐ ┌──────┐ │ │ │
│  │   │ :8000       │          │  │Canvas │ │ GitHub │ │Google│ │ │ │
│  │   └──────▲──────┘          │  └───────┘ └────────┘ └──────┘ │ │ │
│  │          │                 └─────────────────────────────────┘ │ │
│  └──────────┼─────────────────────────────────────────────────────┘ │
│             │ HTTP (localhost:8000)                                  │
│  ┌──────────┴──────────┐                                            │
│  │       AGENT         │  (Claude Code, OpenHands, etc.)            │
│  │  GET  /tools        │  - Fetch tool definitions                  │
│  │  POST /tools/{name} │  - Execute tool calls                      │
│  └─────────────────────┘                                            │
└──────────────────────────────────────────────────────────────────────┘
```

## Credential Flow

```
Setup Phase (once):
  automated_google_setup.sh ──┐
  automated_additional_services.sh ──┼──> configs/token_key_session.py
  Manual Notion/Snowflake setup ─────┘

Runtime (per task):
  Harbor ──> Docker Container
              │
              └─> Tool Server starts
                    │
                    ├─> Reads TOOLATHLON_MCP_SERVERS env var
                    ├─> Loads MCP configs from configs/mcp_servers/
                    ├─> Loads credentials from token_key_session.py
                    └─> Connects to MCP servers
                          │
                          └─> Agent calls tools via HTTP API
```

## Tool Server API

The Tool Server wraps MCP servers and exposes them via HTTP REST:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/tools` | GET | List all tools (OpenAI function format) |
| `/tools/{name}` | POST | Execute a tool call |
| `/servers` | GET | List connected MCP servers |

### Example: List Tools

```bash
curl http://localhost:8000/tools
```

Response:
```json
{
  "tools": [{
    "type": "function",
    "function": {
      "name": "canvas-list_courses",
      "description": "List courses for a user",
      "parameters": {"type": "object", "properties": {...}}
    }
  }],
  "count": 42
}
```

### Example: Call Tool

```bash
curl -X POST http://localhost:8000/tools/canvas-list_courses \
  -H "Content-Type: application/json" \
  -d '{"arguments": {"user_id": "123"}}'
```

## Task Structure

Each converted Harbor task:

```
task_name/
├── instruction.md           # Task description + tool info
├── task.toml                # Harbor configuration
├── environment/
│   ├── Dockerfile           # Task-specific Docker setup
│   └── task_config.json     # MCP servers needed
├── tests/
│   ├── test.sh              # Evaluation script
│   ├── evaluation/          # Python evaluation code
│   └── groundtruth/         # Expected outputs
└── initial_workspace/       # Starting files for agent
```

## Files

| File | Description |
|------|-------------|
| `adapter.py` | Converts Toolathlon tasks to Harbor format |
| `run_adapter.py` | CLI for the adapter |
| `tool_server/server.py` | FastAPI server wrapping MCP |
| `tool_server/startup.sh` | Server startup with health check |
| `docker/Dockerfile.harbor` | Docker image with all MCP dependencies |
| `scripts/setup.sh` | Interactive setup script |
| `docs/SETUP.md` | Detailed setup instructions |

## Supported Agents

Any Harbor agent that can make HTTP requests to the Tool Server:
- Claude Code
- OpenHands
- Codex
- Custom agents

## Troubleshooting

See [docs/SETUP.md#troubleshooting](docs/SETUP.md#troubleshooting) for common issues.

### Quick Checks

```bash
# Check credentials are configured
grep '"XX"' configs/token_key_session.py  # Should return nothing

# Check local services
curl -s http://localhost:20001 && echo "Canvas OK"
curl -s http://localhost:10003 && echo "WooCommerce OK"
curl -s http://localhost:10081 && echo "Email OK"

# Check Docker image
docker images | grep toolathlon-harbor

# Check tool server (inside container)
curl http://localhost:8000/health
```
