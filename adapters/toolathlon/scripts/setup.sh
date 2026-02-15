#!/bin/bash
# Toolathlon Harbor Setup Script
#
# This script orchestrates the full setup process for running Toolathlon with Harbor.
# It wraps the existing Toolathlon setup scripts.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER_DIR="$(dirname "$SCRIPT_DIR")"
TOOLATHLON_ROOT="$(dirname "$(dirname "$ADAPTER_DIR")")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}================================================================${NC}"
echo -e "${BLUE}       Toolathlon Harbor Setup${NC}"
echo -e "${BLUE}================================================================${NC}"
echo ""
echo "Adapter directory: $ADAPTER_DIR"
echo "Toolathlon root: $TOOLATHLON_ROOT"
echo ""

# Verify we're in the right place
if [ ! -f "$TOOLATHLON_ROOT/pyproject.toml" ]; then
    echo -e "${RED}ERROR: Cannot find Toolathlon root at $TOOLATHLON_ROOT${NC}"
    echo "Please run this script from within the Toolathlon repository."
    exit 1
fi

cd "$TOOLATHLON_ROOT"

# =============================================================================
# Step 1: Check/Install Dependencies
# =============================================================================
echo -e "${YELLOW}[Step 1]${NC} Checking dependencies..."

if ! command -v uv &> /dev/null; then
    echo -e "${YELLOW}Installing uv...${NC}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -d ".venv" ]; then
    echo -e "${YELLOW}Installing Python dependencies...${NC}"
    uv sync --frozen
fi

echo -e "${GREEN}✓ Dependencies OK${NC}"
echo ""

# =============================================================================
# Step 2: Credentials Setup
# =============================================================================
echo -e "${YELLOW}[Step 2]${NC} Credentials Setup"
echo ""

# Check if token_key_session.py exists
if [ ! -f "configs/token_key_session.py" ]; then
    echo "Creating token_key_session.py from template..."
    cp configs/token_key_session_example.py configs/token_key_session.py
fi

# Check if credentials are already configured
UNCONFIGURED=$(grep -c '"XX"' configs/token_key_session.py 2>/dev/null || echo "0")

if [ "$UNCONFIGURED" -gt 0 ]; then
    echo -e "${YELLOW}Found $UNCONFIGURED unconfigured credentials in token_key_session.py${NC}"
    echo ""
    echo "Would you like to run the credential setup scripts?"
    echo "  1. Run all setup scripts (Google + GitHub/HF/WandB/Serper)"
    echo "  2. Run Google setup only"
    echo "  3. Run GitHub/HF/WandB/Serper setup only"
    echo "  4. Skip credential setup (configure manually later)"
    echo ""
    read -p "Enter choice [1/2/3/4]: " -n 1 -r
    echo
    echo ""

    case $REPLY in
        1)
            echo -e "${BLUE}Running Google Cloud setup...${NC}"
            bash global_preparation/automated_google_setup.sh
            echo ""
            echo -e "${BLUE}Running additional services setup...${NC}"
            bash global_preparation/automated_additional_services.sh
            ;;
        2)
            echo -e "${BLUE}Running Google Cloud setup...${NC}"
            bash global_preparation/automated_google_setup.sh
            ;;
        3)
            echo -e "${BLUE}Running additional services setup...${NC}"
            bash global_preparation/automated_additional_services.sh
            ;;
        4)
            echo "Skipping credential setup."
            echo "Please configure configs/token_key_session.py manually."
            echo "See docs/SETUP.md for detailed instructions."
            ;;
    esac
else
    echo -e "${GREEN}✓ Credentials appear to be configured${NC}"
fi

echo ""

# =============================================================================
# Step 3: Local Services
# =============================================================================
echo -e "${YELLOW}[Step 3]${NC} Local Services"
echo ""

check_service() {
    local name=$1
    local port=$2
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$port" 2>/dev/null | grep -qE "200|302|301|401|403"; then
        echo -e "  ${GREEN}✓${NC} $name (localhost:$port)"
        return 0
    else
        echo -e "  ${YELLOW}--${NC} $name (localhost:$port) - Not running"
        return 1
    fi
}

echo "Checking local services..."
CANVAS_OK=false
WOOCOMMERCE_OK=false
EMAIL_OK=false

check_service "Canvas LMS" 20001 && CANVAS_OK=true
check_service "WooCommerce" 10003 && WOOCOMMERCE_OK=true
check_service "Email Server" 10081 && EMAIL_OK=true

echo ""

if [ "$CANVAS_OK" = false ] || [ "$WOOCOMMERCE_OK" = false ] || [ "$EMAIL_OK" = false ]; then
    echo "Some local services are not running."
    read -p "Would you like to deploy them now? [y/N]: " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${BLUE}Deploying local services...${NC}"
        bash global_preparation/deploy_containers.sh
    else
        echo "Skipping local service deployment."
        echo "Some tasks may not work without Canvas/WooCommerce/Email services."
    fi
else
    echo -e "${GREEN}✓ All local services running${NC}"
fi

echo ""

# =============================================================================
# Step 4: Build Docker Image
# =============================================================================
echo -e "${YELLOW}[Step 4]${NC} Docker Image"
echo ""

if docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "toolathlon-harbor:latest"; then
    echo -e "${GREEN}✓ Docker image already exists${NC}"
    read -p "Would you like to rebuild it? [y/N]: " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${BLUE}Building Docker image...${NC}"
        docker build -f adapters/toolathlon/docker/Dockerfile.harbor \
            -t toolathlon-harbor:latest .
    fi
else
    echo "Docker image not found."
    read -p "Would you like to build it now? [Y/n]: " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        echo -e "${BLUE}Building Docker image (this may take 5-10 minutes)...${NC}"
        docker build -f adapters/toolathlon/docker/Dockerfile.harbor \
            -t toolathlon-harbor:latest .
    fi
fi

echo ""

# =============================================================================
# Step 5: Convert Tasks
# =============================================================================
echo -e "${YELLOW}[Step 5]${NC} Convert Tasks to Harbor Format"
echo ""

cd "$ADAPTER_DIR"

if [ -d "harbor_tasks" ] && [ "$(ls -A harbor_tasks 2>/dev/null)" ]; then
    TASK_COUNT=$(ls -d harbor_tasks/*/ 2>/dev/null | wc -l)
    echo -e "${GREEN}✓ Harbor tasks already exist ($TASK_COUNT tasks)${NC}"
    read -p "Would you like to regenerate them? [y/N]: " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${BLUE}Converting tasks...${NC}"
        python run_adapter.py --all --output-dir ./harbor_tasks --overwrite
    fi
else
    echo "Converting Toolathlon tasks to Harbor format..."
    python run_adapter.py --all --output-dir ./harbor_tasks --overwrite
fi

echo ""

# =============================================================================
# Summary
# =============================================================================
echo -e "${GREEN}================================================================${NC}"
echo -e "${GREEN}       Setup Complete!${NC}"
echo -e "${GREEN}================================================================${NC}"
echo ""
echo -e "${BLUE}Next steps:${NC}"
echo ""
echo "1. If you skipped credential setup, configure them manually:"
echo "   - Edit: $TOOLATHLON_ROOT/configs/token_key_session.py"
echo "   - Or run: bash $TOOLATHLON_ROOT/global_preparation/automated_google_setup.sh"
echo ""
echo "2. For Notion tasks, complete manual setup:"
echo "   - See: $ADAPTER_DIR/docs/SETUP.md#notion-manual-setup-required"
echo ""
echo "3. For Snowflake tasks, complete manual setup:"
echo "   - See: $ADAPTER_DIR/docs/SETUP.md#snowflake-manual-setup-required"
echo ""
echo "4. Run with Harbor:"
echo "   cd $TOOLATHLON_ROOT"
echo "   harbor run \\"
echo "     --dataset-path adapters/toolathlon/harbor_tasks \\"
echo "     --agent claude-code \\"
echo "     --model anthropic/claude-sonnet-4"
echo ""
