#!/bin/bash
# Toolathlon Tool Server Startup Script
# This script starts the tool server and waits for it to be ready

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${TOOLATHLON_SERVER_LOG:-/logs/agent/tool_server.log}"

echo "Starting Toolathlon Tool Server..."
echo "MCP Servers: ${TOOLATHLON_MCP_SERVERS:-none specified}"
echo "Workspace: ${TOOLATHLON_WORKSPACE:-/app}"

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

# Start the tool server in background
cd /workspace
python -m adapters.toolathlon.tool_server.server > "$LOG_FILE" 2>&1 &
TOOL_SERVER_PID=$!
echo $TOOL_SERVER_PID > /tmp/tool_server.pid

# Wait for server to be ready
MAX_RETRIES=30
RETRY_INTERVAL=1
SERVER_URL="http://localhost:${TOOLATHLON_SERVER_PORT:-8000}/health"

for i in $(seq 1 $MAX_RETRIES); do
    if curl -s "$SERVER_URL" > /dev/null 2>&1; then
        echo "Tool Server is ready! (PID: $TOOL_SERVER_PID)"

        # Print connected servers
        curl -s "http://localhost:${TOOLATHLON_SERVER_PORT:-8000}/servers" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f\"Connected MCP servers: {data.get('connected', [])}\")
"
        exit 0
    fi

    # Check if process is still running
    if ! kill -0 $TOOL_SERVER_PID 2>/dev/null; then
        echo "Tool Server process died. Check logs at $LOG_FILE"
        cat "$LOG_FILE"
        exit 1
    fi

    echo "Waiting for Tool Server... ($i/$MAX_RETRIES)"
    sleep $RETRY_INTERVAL
done

echo "Tool Server failed to start within timeout"
cat "$LOG_FILE"
exit 1
