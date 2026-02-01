# Toolathlon Harbor Integration Plan

## Overview

This document outlines the plan to integrate Toolathlon with the Harbor evaluation framework. The goal is to allow running Toolathlon tasks through Harbor's CLI while supporting Toolathlon's unique requirements (MCP servers, external services, credentials).

## Key Differences Between Terminal-Bench and Toolathlon

| Aspect | Terminal-Bench | Toolathlon |
|--------|---------------|------------|
| Task complexity | Self-contained | Requires external services (Canvas, email, etc.) |
| Tools | Standard CLI tools | MCP servers (40+ different servers) |
| Credentials | Minimal | Complex (Google, GitHub, Notion, Snowflake, Canvas, etc.) |
| Evaluation | Simple bash scripts | Python evaluation scripts |
| Docker setup | Per-task Dockerfile | Shared base image + MCP dependencies |

## Architecture Design

### 1. Directory Structure

```
adapters/toolathlon/
├── adapter.py           # Main adapter converting Toolathlon tasks to Harbor format
├── run_adapter.py       # CLI entry point
├── README.md            # Documentation
├── pyproject.toml       # Dependencies
├── template/
│   ├── task.toml        # Harbor task config template
│   ├── Dockerfile       # Dockerfile template (extends Toolathlon base image)
│   ├── test.sh          # Test script template (wrapper for Python eval)
│   └── instruction.md   # Instruction template
└── toolathlon.yaml      # Registry configuration
```

### 2. Docker Image Strategy

**Option A: Pre-built Base Image (Recommended)**
- Build a single Docker image with all MCP servers and dependencies pre-installed
- Push to registry: `ghcr.io/toolathlon/toolathlon-base:latest`
- Task Dockerfiles simply `FROM` this base image and copy task-specific files
- Credentials passed via environment variables at runtime

**Option B: Build-time Installation**
- Each task builds MCP servers at container start
- Slower but more flexible

### 3. MCP Server Configuration

MCP servers will be configured inside the container via:

1. **Environment Variables** - For credentials (API keys, tokens)
   ```bash
   CANVAS_API_TOKEN=...
   GITHUB_TOKEN=...
   GOOGLE_CLIENT_ID=...
   ```

2. **MCP Config File** - Mounted into container
   ```json
   {
     "servers": {
       "canvas": {
         "command": "npx",
         "args": ["-y", "canvas-mcp-server"],
         "env": {
           "CANVAS_API_TOKEN": "${CANVAS_API_TOKEN}",
           "CANVAS_DOMAIN": "${CANVAS_DOMAIN}"
         }
       }
     }
   }
   ```

### 4. External Services

Toolathlon requires several external services:
- **Canvas LMS** - Learning management system
- **Poste.io** - Email server
- **WooCommerce** - E-commerce platform
- **Local databases** - For various tasks

**Strategy:**
- Services must be running on the host machine before evaluation
- Container connects to host services via Docker networking
- Service endpoints configured via environment variables

### 5. Adapter Implementation

```python
# adapter.py (simplified)
class ToolathlonToHarbor:
    def generate_task(self, task_dir: Path, output_dir: Path):
        # 1. Read Toolathlon task config
        task_config = self.load_task_config(task_dir)

        # 2. Generate instruction.md from docs/task.md + agent_system_prompt.md
        instruction = self.generate_instruction(task_dir)

        # 3. Generate task.toml with appropriate timeouts
        config = self.generate_task_config(task_config)

        # 4. Generate Dockerfile extending base image
        dockerfile = self.generate_dockerfile(task_config)

        # 5. Generate test.sh wrapper for Python evaluation
        test_script = self.generate_test_script(task_dir)

        # 6. Copy necessary files (initial_workspace, groundtruth, etc.)
        self.copy_task_files(task_dir, output_dir)
```

### 6. Test Script Wrapper

```bash
#!/bin/bash
# test.sh - Wrapper for Toolathlon Python evaluation

# Set up environment
export PYTHONPATH=/workspace
cd /workspace

# Run Python evaluation
python evaluation/main.py \
    --agent_workspace /app \
    --groundtruth_workspace /workspace/groundtruth_workspace \
    --res_log_file /logs/verifier/evaluation.log

exit_code=$?

# Write Harbor reward file
mkdir -p /logs/verifier
if [ $exit_code -eq 0 ]; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi

exit $exit_code
```

### 7. Agent Modifications

For Harbor's Claude Code agent to work with Toolathlon:

1. **MCP Server Startup** - Add MCP server initialization to agent setup
2. **Tool Permissions** - Configure allowed MCP tools per task
3. **Working Directory** - Set to `/app` (agent workspace)

## Implementation Steps

### Phase 1: Basic Infrastructure
1. [ ] Create `adapters/toolathlon/` directory structure
2. [ ] Build Toolathlon base Docker image with all dependencies
3. [ ] Implement basic adapter that converts task structure
4. [ ] Create test.sh wrapper template

### Phase 2: MCP Integration
5. [ ] Implement MCP server configuration generation
6. [ ] Handle credential injection via environment variables
7. [ ] Test with simple tasks (filesystem-only)

### Phase 3: External Services
8. [ ] Document external service requirements
9. [ ] Add Docker networking configuration for host services
10. [ ] Test with Canvas-dependent tasks

### Phase 4: Full Integration
11. [ ] Register Toolathlon in Harbor registry
12. [ ] Test full evaluation pipeline
13. [ ] Add support for parallel execution
14. [ ] Document usage

## Configuration Examples

### Harbor Run Command
```bash
# Run Toolathlon with Harbor
export TOOLATHLON_CANVAS_TOKEN=...
export TOOLATHLON_GITHUB_TOKEN=...
# ... other credentials

harbor run \
    --dataset toolathlon@1.0 \
    --agent claude-code \
    --model anthropic/claude-opus-4-1 \
    --n-concurrent 4 \
    --env docker
```

### Task-Specific Environment
```yaml
# toolathlon.yaml (registry entry)
name: toolathlon
versions:
  "1.0":
    git_url: "https://github.com/yourorg/toolathlon-harbor-tasks.git"
    git_ref: "main"
    metrics:
      - type: mean
```

## Ignored/Deferred Features

Per user requirements:
- `handle_overlong_tool_outputs` local tool - Ignored (part of default harness)
- Daytona cloud execution - Explore later
- Custom Toolathlon harness - Replaced entirely by Harbor

## Questions/Decisions Needed

1. **Credential Management**: How should credentials be securely passed?
   - Environment variables at runtime
   - Secrets management service
   - Mounted credential files

2. **External Service Hosting**: Where should Canvas/Poste/etc run?
   - Same machine as Harbor
   - Separate server with network access
   - Cloud-hosted services

3. **Task Selection**: Should all 110+ tasks be adapted or start with a subset?

4. **Base Image Publishing**: Where to host the base Docker image?
   - GitHub Container Registry
   - Docker Hub
   - Private registry

## File Mapping

| Toolathlon | Harbor |
|------------|--------|
| `docs/task.md` | `instruction.md` |
| `docs/agent_system_prompt.md` | Prepended to `instruction.md` |
| `task_config.json` | `task.toml` + MCP config |
| `evaluation/main.py` | `tests/test.sh` (wrapper) |
| `initial_workspace/` | Copied to `/app` at runtime |
| `groundtruth_workspace/` | `tests/groundtruth/` |
| `Dockerfile` (root) | Base image, extended per-task |

## Next Steps

1. Create the adapter directory structure
2. Build and test base Docker image
3. Implement adapter for a single task
4. Test end-to-end with Harbor CLI
