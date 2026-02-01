#!/bin/bash
# MCP Server Setup Script for Toolathlon
# This script configures MCP servers based on the task's requirements

set -e

MCP_CONFIG_FILE="${MCP_CONFIG_FILE:-/workspace/mcp_config.json}"

if [ ! -f "$MCP_CONFIG_FILE" ]; then
    echo "Warning: MCP config file not found at $MCP_CONFIG_FILE"
    exit 0
fi

echo "Setting up MCP servers from $MCP_CONFIG_FILE..."

# Read needed MCP servers from config
SERVERS=$(jq -r '.needed_mcp_servers[]' "$MCP_CONFIG_FILE" 2>/dev/null || echo "")

for SERVER in $SERVERS; do
    echo "Configuring MCP server: $SERVER"

    case $SERVER in
        "canvas")
            echo "  Canvas server configured via CANVAS_API_TOKEN and CANVAS_DOMAIN env vars"
            ;;
        "filesystem")
            echo "  Filesystem server ready"
            ;;
        "github")
            echo "  GitHub server configured via GITHUB_TOKEN env var"
            ;;
        "google-cloud")
            echo "  Google Cloud server configured via GCP credentials"
            ;;
        "emails")
            echo "  Emails server configured via emails config"
            ;;
        *)
            echo "  Server $SERVER - using default configuration"
            ;;
    esac
done

echo "MCP server setup complete"
