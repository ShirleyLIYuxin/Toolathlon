# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Toolathlon is a benchmark framework for evaluating language agents' tool use capabilities. It features 600+ diverse tools across realistic software environments (Canvas LMS, GitHub, Google Suite, email, etc.) with 110+ long-horizon tasks requiring multi-step tool calls.

## Development Setup

### Prerequisites
- Python 3.12.11 (exact version required)
- uv package manager
- Docker or Podman

### Installation
```bash
# Install uv if not present
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install all dependencies
bash global_preparation/install_env_minimal.sh [true|false]  # true if you have sudo

# Pull container image
bash global_preparation/pull_toolathlon_image.sh

# Deploy local services (Canvas, email server, etc.)
bash global_preparation/deploy_containers.sh [true|false]
```

### Environment Variables
```bash
export TOOLATHLON_OPENAI_API_KEY="your-api-key"
export TOOLATHLON_OPENAI_BASE_URL="https://your-endpoint.com/v1"
```

## Running Tasks

### Single Task (Development)
```bash
# Direct Python execution
python main.py --task_dir tasks/finalpool/{taskname} --model_short_name {model} --provider unified

# Containerized execution (recommended)
bash scripts/run_single_containerized.sh finalpool/{taskname} quickstart {dump_path} {model}
```

### Parallel Evaluation
```bash
bash scripts/run_parallel.sh {model} {dump_path} unified {num_workers}
```

### Remote Evaluation Service
```bash
# Public API mode
python eval_client.py run --mode public --base-url https://api.openai.com/v1 --model-name {model} --server-host 47.253.6.47 --api-key {key}

# Private/local model mode
python eval_client.py run --mode private --base-url http://localhost:8000/v1 --model-name {model} --server-host 47.253.6.47
```

### Trajectory Visualization
```bash
uv run vis_traj/server.py --port 8000 --res_path {dump_path}/finalpool/
```

## Architecture

### Key Entry Points
- `main.py` - Single task execution
- `run_parallel.py` - Parallel batch orchestrator
- `eval_server.py` / `eval_client.py` - Remote evaluation service

### Core Modules (`utils/`)
- `roles/task_agent.py` - Main agent orchestrator handling tool calls and conversation loop
- `mcp/tool_servers.py` - MCPServerManager for MCP server lifecycle
- `task_runner/runner.py` - TaskRunner orchestrates task execution pipeline
- `api_model/model_provider.py` - Multi-provider LLM abstraction (OpenAI, Anthropic, etc.)
- `data_structures/` - Configuration data models (TaskConfig, AgentConfig, MCPConfig)
- `evaluation/evaluator.py` - Runs task-specific evaluation scripts
- `app_specific/` - App integration wrappers (Canvas, GitHub, Google, etc.)
- `aux_tools/` - Local tools (sleep, claim_done, python_execute, web_search, etc.)

### Configuration System
1. **Global configs** (`configs/global_configs.py`) - API keys, container runtime
2. **Token/credentials** (`configs/token_key_session.py`) - App-specific tokens
3. **Instance config** (`configs/instance.py`) - Port numbers, instance identification for multi-instance deployments
4. **MCP servers** (`configs/mcp_servers/*.yaml`) - YAML-based server definitions with template substitution

### Task Structure
Each task in `tasks/finalpool/{task}/` contains:
- `docs/task.md` - User-facing task description
- `docs/agent_system_prompt.md` - Agent instructions
- `task_config.json` - MCP servers and tools needed
- `evaluation/main.py` - Custom evaluation script
- `initial_workspace/` - Starting files for agent
- `groundtruth_workspace/` - Expected outputs for verification

### MCP Server Configuration
YAML configs support variable substitution:
- `${agent_workspace}` - Task execution directory
- `${config.*}` - Values from global_configs
- `${token.*}` - Credentials from token_key_session
- `${instance.*}` - Instance-specific values (ports, instance_suffix) from instance config

## Key Patterns

### Agent Execution Loop
1. Load task config and system prompts
2. Initialize MCP servers on demand
3. Run agent loop: chat completion → tool calls → tool execution
4. Check termination conditions (user stop phrase, claim_done tool, max turns)
5. Run task-specific evaluation script

### Adding New Tasks
Create directory in `tasks/finalpool/{taskname}/` with standard structure including task description, prompts, config, and evaluation script.

### Adding New MCP Servers
Create YAML config in `configs/mcp_servers/` specifying command, args, and environment variables.

### Model Providers
The `unified` provider works with any OpenAI-compatible API. Use `openai_stateful_responses` for OpenAI's Responses API with automatic context management.

## Output Structure
Results are saved to dump directories:
- `{dump_path}/finalpool/{task}/` - Per-task logs and artifacts
- `eval_stats.json` - Aggregated pass/fail statistics
- `traj_log_all.jsonl` - All task trajectories

## Multi-Instance Deployment

To run multiple Toolathlon evaluations on the same machine without port conflicts:

### Key Files
- `configs/instance.py` - Instance config loader (reads from `TOOLATHLON_INSTANCE_CONFIG` env var or `configs/instance.yaml`)
- `configs/instance.example.yaml` - Template for instance configuration
- `configs/load_instance_env.sh` - Shell helper to export instance config as env vars
- `utils/general/template_processor.py` - Shared template variable processor
- `global_preparation/migrate_ports_to_variables.py` - Converts hardcoded ports to `${instance.*}` variables

### Running Two Instances
```bash
# Instance A: default ports
bash global_preparation/deploy_containers.sh
python eval_server.py 8080 8081

# Instance B: alternate ports
export TOOLATHLON_INSTANCE_CONFIG=configs/instance_b.yaml
bash global_preparation/deploy_containers.sh
python eval_server.py 8082 8083
```

### Port Configuration
Instance config supports these port variables:
- `port_canvas_http` (default: 10001)
- `port_canvas_https` (default: 20001)
- `port_imap` (default: 1143)
- `port_smtp` (default: 2525)
- `port_smtp_submission` (default: 1587)
- `port_email_web` (default: 10005)
- `port_woocommerce` (default: 10003)

JSON configs use `${instance.port_*}` variables that are resolved at runtime based on the active instance config.

## Harbor Integration

Toolathlon can be run through the Harbor evaluation framework. This is the recommended approach for standardized benchmarking.

### Harbor Adapter Location
`adapters/toolathlon/` - Contains the adapter that converts Toolathlon tasks to Harbor format

### Running with Harbor

1. **Convert tasks to Harbor format:**
```bash
cd adapters/toolathlon
python run_adapter.py --all --output-dir ./harbor_tasks
```

2. **Run with Harbor CLI:**
```bash
harbor run \
    --dataset-path ./harbor_tasks \
    --agent claude-code \
    --model anthropic/claude-opus-4-1 \
    --n-concurrent 4
```

### Harbor Task Structure
After conversion, each task has:
- `instruction.md` - Task description for agent
- `task.toml` - Harbor configuration
- `environment/Dockerfile` - Extends Toolathlon base image
- `environment/mcp_config.json` - MCP server requirements
- `tests/test.sh` - Evaluation wrapper
- `tests/evaluation/` - Python evaluation scripts
- `tests/groundtruth/` - Expected outputs

### Building the Base Docker Image
```bash
cd adapters/toolathlon/docker
docker build -f Dockerfile.base -t ghcr.io/toolathlon/toolathlon-base:latest .
```

### Key Files
- `adapters/toolathlon/adapter.py` - Main conversion logic
- `adapters/toolathlon/run_adapter.py` - CLI entry point
- `adapters/toolathlon/docker/Dockerfile.base` - Base image with MCP dependencies
- `HARBOR_INTEGRATION_PLAN.md` - Detailed integration documentation
