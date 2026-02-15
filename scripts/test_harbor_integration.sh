#!/bin/bash
# =============================================================================
# Toolathlon Harbor Integration Test Script
# Run on CPU machine with Docker installed
#
# Usage:
#   # Step 0: Set required env vars first
#   export ANTHROPIC_API_KEY="sk-ant-..."          # or use OpenRouter
#   export TOOLATHLON_OPENAI_API_KEY="sk-or-..."   # OpenRouter key
#   export TOOLATHLON_OPENAI_BASE_URL="https://openrouter.ai/api/v1"
#
#   # Then run specific stages:
#   bash scripts/test_harbor_integration.sh setup       # Stage 1: install deps + convert tasks
#   bash scripts/test_harbor_integration.sh build       # Stage 2: build Docker image
#   bash scripts/test_harbor_integration.sh smoke       # Stage 3: tool_server smoke test in container
#   bash scripts/test_harbor_integration.sh harbor      # Stage 4: Harbor + Docker (1 task)
#   bash scripts/test_harbor_integration.sh harbor-batch # Stage 5: Harbor + Docker (multiple tasks)
#   bash scripts/test_harbor_integration.sh all          # Run stages 1-4 sequentially
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLATHLON_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ADAPTER_DIR="$TOOLATHLON_ROOT/adapters/toolathlon"
HARBOR_TASKS_DIR="$ADAPTER_DIR/harbor_tasks"
HARBOR_REPO="${HARBOR_REPO:-$TOOLATHLON_ROOT/../harbor}"
DOCKER_IMAGE="toolathlon-harbor:latest"

# Simple tasks for testing (local-only, no credentials needed)
SMOKE_TASK="invoice-org"
SIMPLE_TASKS=("invoice-org" "arrange-workspace" "cooking-guidance")

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: Setup — install deps, convert tasks
# ─────────────────────────────────────────────────────────────────────────────
stage_setup() {
    log "=== Stage 1: Setup ==="

    # Check prerequisites
    log "Checking prerequisites..."
    command -v docker >/dev/null 2>&1 || { err "Docker not found"; exit 1; }
    command -v uv >/dev/null 2>&1 || { err "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
    command -v python3 >/dev/null 2>&1 || { err "python3 not found"; exit 1; }
    ok "Prerequisites: docker, uv, python3"

    # Install Python deps
    log "Installing Toolathlon Python dependencies..."
    cd "$TOOLATHLON_ROOT"
    uv sync --frozen 2>&1 | tail -3
    ok "Python dependencies installed"

    # Convert tasks
    log "Converting all tasks to Harbor format..."
    cd "$ADAPTER_DIR"
    uv run --project "$TOOLATHLON_ROOT" python run_adapter.py --all --output-dir ./harbor_tasks --overwrite 2>&1 | tail -5

    TASK_COUNT=$(ls -d "$HARBOR_TASKS_DIR"/*/ 2>/dev/null | wc -l | tr -d ' ')
    ok "Converted $TASK_COUNT tasks to $HARBOR_TASKS_DIR"
}

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: Build Docker image
# ─────────────────────────────────────────────────────────────────────────────
stage_build() {
    log "=== Stage 2: Build Docker Image ==="

    cd "$TOOLATHLON_ROOT"

    # Check if image already exists
    if docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
        warn "Image $DOCKER_IMAGE already exists. Use 'docker rmi $DOCKER_IMAGE' to rebuild."
        read -p "Skip build? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            ok "Skipping build"
            return 0
        fi
    fi

    log "Building $DOCKER_IMAGE (this may take 10-20 minutes on first build)..."
    log "Build context: $TOOLATHLON_ROOT"

    docker build \
        -f adapters/toolathlon/docker/Dockerfile.harbor \
        -t "$DOCKER_IMAGE" \
        . 2>&1 | tee /tmp/toolathlon-docker-build.log | tail -20

    if [ ${PIPESTATUS[0]} -eq 0 ]; then
        ok "Docker image built: $DOCKER_IMAGE"
        docker image inspect "$DOCKER_IMAGE" --format '  Size: {{.Size}} bytes'
    else
        err "Docker build failed. Full log: /tmp/toolathlon-docker-build.log"
        exit 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: Smoke test — tool_server inside container
# ─────────────────────────────────────────────────────────────────────────────
stage_smoke() {
    log "=== Stage 3: Smoke Test — Tool Server in Container ==="

    # Verify image exists
    if ! docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
        err "Image $DOCKER_IMAGE not found. Run 'build' stage first."
        exit 1
    fi

    # Verify converted tasks exist
    if [ ! -d "$HARBOR_TASKS_DIR/$SMOKE_TASK" ]; then
        err "Task $SMOKE_TASK not found. Run 'setup' stage first."
        exit 1
    fi

    CONTAINER_NAME="toolathlon-smoke-test"

    # Cleanup any previous container
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

    log "Starting container with filesystem + pdf-tools MCP servers..."
    docker run -d \
        --name "$CONTAINER_NAME" \
        -e TOOLATHLON_MCP_SERVERS="filesystem,pdf-tools" \
        -e TOOLATHLON_WORKSPACE="/app" \
        -p 8000:8000 \
        "$DOCKER_IMAGE" \
        bash -c "source .venv/bin/activate && python adapters/toolathlon/tool_server/server.py" \
        2>&1

    log "Waiting for tool_server to start (up to 30s)..."
    for i in $(seq 1 30); do
        if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
            ok "Tool server is healthy!"
            break
        fi
        if [ $i -eq 30 ]; then
            err "Tool server failed to start in 30s"
            echo "Container logs:"
            docker logs "$CONTAINER_NAME" 2>&1 | tail -30
            docker rm -f "$CONTAINER_NAME" 2>/dev/null
            exit 1
        fi
        sleep 1
    done

    # Test health endpoint
    log "Testing endpoints..."
    echo "  /health:"
    curl -s http://localhost:8000/health | python3 -m json.tool 2>/dev/null || curl -s http://localhost:8000/health

    # Test tools list
    echo ""
    TOOL_COUNT=$(curl -s http://localhost:8000/tools | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])" 2>/dev/null || echo "?")
    ok "Tools available: $TOOL_COUNT"

    # Test actual tool call
    echo ""
    log "Testing tool call: filesystem-list_directory /workspace"
    RESULT=$(curl -s -X POST http://localhost:8000/tools/filesystem-list_directory \
        -H "Content-Type: application/json" \
        -d '{"arguments":{"path":"/workspace"}}' 2>&1)
    if echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['success']" 2>/dev/null; then
        ok "Tool call succeeded"
        echo "$RESULT" | python3 -m json.tool 2>/dev/null | head -10
    else
        warn "Tool call returned: $RESULT"
    fi

    # Cleanup
    docker rm -f "$CONTAINER_NAME" 2>/dev/null
    ok "Smoke test complete"
}

# ─────────────────────────────────────────────────────────────────────────────
# Stage 4: Harbor + Docker — single task end-to-end
# ─────────────────────────────────────────────────────────────────────────────
stage_harbor() {
    log "=== Stage 4: Harbor + Docker (single task) ==="

    # Check Harbor repo
    if [ ! -d "$HARBOR_REPO" ]; then
        err "Harbor repo not found at $HARBOR_REPO"
        err "Set HARBOR_REPO env var or clone it next to Toolathlon"
        exit 1
    fi

    # Check converted tasks
    if [ ! -d "$HARBOR_TASKS_DIR/$SMOKE_TASK" ]; then
        err "Converted task $SMOKE_TASK not found. Run 'setup' stage first."
        exit 1
    fi

    # Check for LLM API key
    if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${TOOLATHLON_OPENAI_API_KEY:-}" ]; then
        err "No LLM API key set. Export ANTHROPIC_API_KEY or TOOLATHLON_OPENAI_API_KEY"
        exit 1
    fi

    # Determine agent and model
    AGENT="${HARBOR_AGENT:-claude-code}"
    MODEL="${HARBOR_MODEL:-anthropic/claude-sonnet-4.5}"

    log "Task:  $SMOKE_TASK"
    log "Agent: $AGENT"
    log "Model: $MODEL"
    log "Path:  $HARBOR_TASKS_DIR/$SMOKE_TASK"

    cd "$HARBOR_REPO"

    log "Running Harbor..."
    uv run harbor run \
        --path "$HARBOR_TASKS_DIR/$SMOKE_TASK" \
        --agent "$AGENT" \
        --model "$MODEL" \
        --env docker \
        --n-concurrent 1 \
        --n-attempts 1 \
        --job-name "toolathlon-test-${SMOKE_TASK}" \
        --jobs-dir "$TOOLATHLON_ROOT/results/harbor" \
        2>&1

    # Check results
    RESULT_DIR="$TOOLATHLON_ROOT/results/harbor"
    if ls "$RESULT_DIR"/toolathlon-test-* >/dev/null 2>&1; then
        ok "Harbor run complete. Results in: $RESULT_DIR"
        # Try to show reward
        find "$RESULT_DIR" -name "reward.txt" -exec sh -c 'echo "  Reward: $(cat {})"' \;
    else
        warn "No results found in $RESULT_DIR — check Harbor output above"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Stage 5: Harbor + Docker — batch (multiple simple tasks)
# ─────────────────────────────────────────────────────────────────────────────
stage_harbor_batch() {
    log "=== Stage 5: Harbor + Docker (batch) ==="

    if [ ! -d "$HARBOR_REPO" ]; then
        err "Harbor repo not found at $HARBOR_REPO"
        exit 1
    fi

    # Check for API key
    if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${TOOLATHLON_OPENAI_API_KEY:-}" ]; then
        err "No LLM API key set."
        exit 1
    fi

    AGENT="${HARBOR_AGENT:-claude-code}"
    MODEL="${HARBOR_MODEL:-anthropic/claude-sonnet-4.5}"
    CONCURRENT="${HARBOR_CONCURRENT:-2}"

    log "Tasks: ${SIMPLE_TASKS[*]}"
    log "Agent: $AGENT | Model: $MODEL | Concurrent: $CONCURRENT"

    cd "$HARBOR_REPO"

    # Run the full harbor_tasks directory with task name filtering
    TASK_NAMES=""
    for t in "${SIMPLE_TASKS[@]}"; do
        TASK_NAMES="$TASK_NAMES -t $t"
    done

    log "Running Harbor batch..."
    uv run harbor run \
        --path "$HARBOR_TASKS_DIR" \
        $TASK_NAMES \
        --agent "$AGENT" \
        --model "$MODEL" \
        --env docker \
        --n-concurrent "$CONCURRENT" \
        --n-attempts 1 \
        --job-name "toolathlon-batch" \
        --jobs-dir "$TOOLATHLON_ROOT/results/harbor" \
        2>&1

    RESULT_DIR="$TOOLATHLON_ROOT/results/harbor"
    ok "Batch run complete. Results in: $RESULT_DIR"
    find "$RESULT_DIR" -name "reward.txt" -exec sh -c 'echo "  $(dirname {} | xargs basename): reward=$(cat {})"' \;
}

# ─────────────────────────────────────────────────────────────────────────────
# Run all stages
# ─────────────────────────────────────────────────────────────────────────────
stage_all() {
    stage_setup
    echo ""
    stage_build
    echo ""
    stage_smoke
    echo ""
    stage_harbor
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
STAGE="${1:-help}"

case "$STAGE" in
    setup)        stage_setup ;;
    build)        stage_build ;;
    smoke)        stage_smoke ;;
    harbor)       stage_harbor ;;
    harbor-batch) stage_harbor_batch ;;
    all)          stage_all ;;
    *)
        echo "Toolathlon Harbor Integration Test"
        echo ""
        echo "Usage: bash $0 <stage>"
        echo ""
        echo "Stages (run in order):"
        echo "  setup        Install deps, convert 108 tasks to Harbor format"
        echo "  build        Build toolathlon-harbor Docker image (~10-20 min first time)"
        echo "  smoke        Smoke test: run tool_server in container, test with curl"
        echo "  harbor       End-to-end: Harbor + Docker, 1 simple task"
        echo "  harbor-batch End-to-end: Harbor + Docker, multiple tasks"
        echo "  all          Run setup → build → smoke → harbor"
        echo ""
        echo "Required env vars:"
        echo "  ANTHROPIC_API_KEY     For Claude Code agent"
        echo "  (or) OPENAI_API_KEY   For OpenAI-compatible agent"
        echo ""
        echo "Optional env vars:"
        echo "  HARBOR_REPO           Path to harbor repo (default: ../harbor)"
        echo "  HARBOR_AGENT          Agent name (default: claude-code)"
        echo "  HARBOR_MODEL          Model ID (default: anthropic/claude-sonnet-4.5)"
        echo "  HARBOR_CONCURRENT     Concurrent trials for batch (default: 2)"
        echo ""
        echo "Example:"
        echo "  export ANTHROPIC_API_KEY='sk-ant-...'"
        echo "  bash $0 all"
        ;;
esac
