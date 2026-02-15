# Toolathlon Harbor Setup Guide

This guide walks through setting up Toolathlon for evaluation with Harbor. The setup takes approximately 30 minutes.

## Prerequisites

- Python 3.12.11
- [uv](https://docs.astral.sh/uv/) package manager
- Docker
- gcloud CLI (for Google services)

## Overview

Toolathlon requires credentials for various services:

### Remote Services (API keys needed)
- **Google Cloud** - Sheets, Forms, Drive, Calendar, BigQuery, etc.
- **GitHub** - Repository operations
- **Notion** - Database operations
- **Snowflake** - Data warehouse operations
- **WandB** - ML experiment tracking
- **HuggingFace** - Dataset/model operations
- **Serper** - Web search

### Local Services (deployed via Docker)
- **Canvas LMS** - Learning management system
- **WooCommerce** - E-commerce platform
- **Poste.io** - Email server

## Setup Steps

### Step 1: Install Toolathlon Environment

From the Toolathlon root directory:

```bash
# Install uv if not present
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
bash global_preparation/install_env_minimal.sh
```

### Step 2: Create token_key_session.py

```bash
# Copy the example file
cp configs/token_key_session_example.py configs/token_key_session.py
```

### Step 3: Set Up Remote Service Credentials

#### Google Cloud (Required for most tasks)

This automated script will:
- Create/select a GCP project
- Enable required APIs
- Create service account and keys
- Set up OAuth credentials
- Auto-fill token_key_session.py

```bash
bash global_preparation/automated_google_setup.sh
```

#### GitHub, HuggingFace, WandB, Serper

This script guides you through:
- Creating accounts (if needed)
- Generating API tokens
- Auto-filling token_key_session.py

```bash
bash global_preparation/automated_additional_services.sh
```

#### Notion (Manual setup required)

1. Create a Notion workspace (recommend using your Toolathlon Google account)

2. Connect to Notion's official MCP:
   ```bash
   uv run -m global_preparation.special_setup_notion_official
   ```

3. Duplicate the [Notion Source Page](https://amazing-wave-b38.notion.site/Notion-Source-Page-27ad10a48436805b9179fdaff2f65be2) to your workspace

4. Create a new page called "Notion Eval Page" in your workspace

5. Create an [Integration](https://www.notion.so/profile/integrations):
   - Add both pages to the integration's access
   - Copy the "Internal Integration Secret"
   - Add to `configs/token_key_session.py`:
     ```python
     notion_integration_key = "your_key"
     source_notion_page_url = "your_source_page_url"
     eval_notion_page_url = "your_eval_page_url"
     ```

6. Create a second integration for evaluation only (access only to Eval Page):
   ```python
   notion_integration_key_eval = "your_eval_only_key"
   ```

#### Snowflake (Manual setup required)

1. Create account at [Snowflake](https://signup.snowflake.com/)

2. Generate RSA key pair:
   ```bash
   # Generate private key
   openssl genrsa 2048 | openssl pkcs8 -topk8 -v2 des3 -inform PEM -out ./configs/snowflake_rsa_key.p8 -nocrypt

   # Generate public key
   openssl rsa -in ./configs/snowflake_rsa_key.p8 -pubout -out ./configs/snowflake_rsa_key.pub

   # Display public key (for Snowflake console)
   cat ./configs/snowflake_rsa_key.pub | grep -v "BEGIN PUBLIC KEY" | grep -v "END PUBLIC KEY" | tr -d '\n'; echo
   ```

3. In Snowflake SQL console:
   ```sql
   ALTER USER YOUR_USERNAME SET RSA_PUBLIC_KEY='your_public_key_here';
   ```

4. Update `configs/token_key_session.py`:
   ```python
   snowflake_account = "your_account"
   snowflake_user = "your_username"
   snowflake_warehouse = "COMPUTE_WH"
   snowflake_role = "ACCOUNTADMIN"
   ```

### Step 4: Deploy Local Services

```bash
bash global_preparation/deploy_containers.sh
```

This starts:
- Canvas LMS at `localhost:20001`
- WooCommerce at `localhost:10003`
- Poste.io email at `localhost:10081`

### Step 5: Build Harbor Docker Image

```bash
# From Toolathlon root
docker build -f adapters/toolathlon/docker/Dockerfile.harbor \
    -t toolathlon-harbor:latest .
```

### Step 6: Convert Tasks to Harbor Format

```bash
cd adapters/toolathlon
python run_adapter.py --all --output-dir ./harbor_tasks --overwrite
```

### Step 7: Run with Harbor

```bash
# Run single task
harbor run \
    --dataset-path adapters/toolathlon/harbor_tasks/canvas-do-quiz \
    --agent claude-code \
    --model anthropic/claude-sonnet-4

# Run all tasks
harbor run \
    --dataset-path adapters/toolathlon/harbor_tasks \
    --agent claude-code \
    --model anthropic/claude-sonnet-4 \
    --n-concurrent 4
```

## Credential Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    SETUP (done once)                             │
│                                                                  │
│  1. User runs setup scripts                                      │
│     └─> configs/token_key_session.py is populated               │
│                                                                  │
│  2. Docker build                                                 │
│     └─> Entire Toolathlon repo copied to /workspace             │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                    RUNTIME (per task)                            │
│                                                                  │
│  Harbor passes env vars ──> Docker Container                     │
│                                                                  │
│  In container:                                                   │
│    Tool Server starts                                            │
│       │                                                          │
│       ├─> Reads TOOLATHLON_MCP_SERVERS env var                  │
│       │                                                          │
│       ├─> Loads MCP configs from configs/mcp_servers/*.yaml     │
│       │                                                          │
│       ├─> Credentials come from:                                 │
│       │   1. Environment variables (preferred)                   │
│       │   2. configs/token_key_session.py (fallback)            │
│       │                                                          │
│       └─> MCPServerManager connects to MCP servers              │
│                                                                  │
│  Agent calls HTTP API ──> Tool Server ──> MCP Servers           │
└─────────────────────────────────────────────────────────────────┘
```

## Environment Variable Mapping

The Tool Server maps environment variables to credential keys:

| Environment Variable | token_key_session.py key |
|---------------------|--------------------------|
| `GOOGLE_CLIENT_ID` | `google_client_id` |
| `GOOGLE_CLIENT_SECRET` | `google_client_secret` |
| `GOOGLE_REFRESH_TOKEN` | `google_refresh_token` |
| `GCP_PROJECT_ID` | `gcp_project_id` |
| `GITHUB_TOKEN` | `github_token` |
| `CANVAS_API_TOKEN` | `canvas_api_token` |
| `CANVAS_DOMAIN` | `canvas_domain` |
| `NOTION_INTEGRATION_KEY` | `notion_integration_key` |
| `SNOWFLAKE_ACCOUNT` | `snowflake_account` |
| `SNOWFLAKE_USER` | `snowflake_user` |
| `WANDB_API_KEY` | `wandb_api_key` |
| `HUGGINGFACE_TOKEN` | `huggingface_token` |
| `SERPER_API_KEY` | `serper_api_key` |

## Verifying Setup

### Check credentials file
```bash
# Should show all XX replaced with actual values
grep "XX" configs/token_key_session.py
# (should return no results if fully configured)
```

### Check local services
```bash
curl -s http://localhost:20001 && echo "Canvas OK"
curl -s http://localhost:10003 && echo "WooCommerce OK"
curl -s http://localhost:10081 && echo "Email OK"
```

### Test tool server locally
```bash
cd /path/to/Toolathlon
export TOOLATHLON_MCP_SERVERS="canvas"
export TOOLATHLON_WORKSPACE="/tmp/test"
python -m adapters.toolathlon.tool_server.server

# In another terminal:
curl http://localhost:8000/health
curl http://localhost:8000/tools
```

## Troubleshooting

### Setup script errors
- Make sure you're running from the Toolathlon root directory
- Ensure `uv` is installed and in PATH
- Check that gcloud CLI is authenticated

### Docker build fails
- Ensure you have Docker installed and running
- Check disk space (image is ~5GB)
- Try with `--no-cache` flag

### MCP server connection errors
- Verify credentials in `token_key_session.py`
- Check that local services are running (for Canvas/WooCommerce/Email tasks)
- Look at `/logs/agent/tool_server.log` for detailed errors

### Task-specific credential issues
Some tasks override credentials. Check `tasks/finalpool/{task}/token_key_session.py` for task-specific values that get merged with global credentials.
