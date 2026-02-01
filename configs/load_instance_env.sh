#!/bin/bash
# Load instance configuration as environment variables
#
# Usage:
#   source configs/load_instance_env.sh
#   echo $TOOLATHLON_PORT_IMAP  # 1143 or configured value
#
# Or in another script:
#   source "$(dirname "$0")/../configs/load_instance_env.sh"

# Default values
export TOOLATHLON_PORT_CANVAS_HTTP=${TOOLATHLON_PORT_CANVAS_HTTP:-10001}
export TOOLATHLON_PORT_CANVAS_HTTPS=${TOOLATHLON_PORT_CANVAS_HTTPS:-20001}
export TOOLATHLON_PORT_IMAP=${TOOLATHLON_PORT_IMAP:-1143}
export TOOLATHLON_PORT_SMTP=${TOOLATHLON_PORT_SMTP:-2525}
export TOOLATHLON_PORT_SMTP_SUBMISSION=${TOOLATHLON_PORT_SMTP_SUBMISSION:-1587}
export TOOLATHLON_PORT_EMAIL_WEB=${TOOLATHLON_PORT_EMAIL_WEB:-10005}
export TOOLATHLON_PORT_WOOCOMMERCE=${TOOLATHLON_PORT_WOOCOMMERCE:-10003}
export TOOLATHLON_PORT_K8S_PR_PREVIEW=${TOOLATHLON_PORT_K8S_PR_PREVIEW:-30123}
export TOOLATHLON_PORT_K8S_MYSQL=${TOOLATHLON_PORT_K8S_MYSQL:-30124}
export TOOLATHLON_PORT_MEETING_ASSIGN=${TOOLATHLON_PORT_MEETING_ASSIGN:-30137}
export TOOLATHLON_INSTANCE_PREFIX=${TOOLATHLON_INSTANCE_PREFIX:-""}
export TOOLATHLON_INSTANCE_SUFFIX=${TOOLATHLON_INSTANCE_SUFFIX:-""}

# If instance config file exists, load values from it
INSTANCE_CONFIG="${TOOLATHLON_INSTANCE_CONFIG:-configs/instance.yaml}"

if [[ -f "$INSTANCE_CONFIG" ]]; then
    echo "[Instance Config] Loading from: $INSTANCE_CONFIG"

    # Parse YAML using Python (more reliable than shell parsing)
    if command -v python3 &> /dev/null; then
        eval "$(python3 - "$INSTANCE_CONFIG" << 'PYTHON_SCRIPT'
import sys
import yaml

config_path = sys.argv[1]
try:
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    # Map YAML keys to env var names
    mappings = {
        'port_canvas_http': 'TOOLATHLON_PORT_CANVAS_HTTP',
        'port_canvas_https': 'TOOLATHLON_PORT_CANVAS_HTTPS',
        'port_imap': 'TOOLATHLON_PORT_IMAP',
        'port_smtp': 'TOOLATHLON_PORT_SMTP',
        'port_smtp_submission': 'TOOLATHLON_PORT_SMTP_SUBMISSION',
        'port_email_web': 'TOOLATHLON_PORT_EMAIL_WEB',
        'port_woocommerce': 'TOOLATHLON_PORT_WOOCOMMERCE',
        'port_k8s_pr_preview': 'TOOLATHLON_PORT_K8S_PR_PREVIEW',
        'port_k8s_mysql': 'TOOLATHLON_PORT_K8S_MYSQL',
        'port_meeting_assign': 'TOOLATHLON_PORT_MEETING_ASSIGN',
        'instance_prefix': 'TOOLATHLON_INSTANCE_PREFIX',
        'instance_suffix': 'TOOLATHLON_INSTANCE_SUFFIX',
    }

    for yaml_key, env_var in mappings.items():
        if yaml_key in config:
            value = config[yaml_key]
            # Escape single quotes in value
            if isinstance(value, str):
                value = value.replace("'", "'\\''")
            print(f"export {env_var}='{value}'")

except Exception as e:
    print(f"# Warning: Failed to parse {config_path}: {e}", file=sys.stderr)
PYTHON_SCRIPT
)"
    else
        echo "[Instance Config] Warning: Python3 not found, using default values"
    fi
fi

# Export array of all required ports for convenience
export TOOLATHLON_REQUIRED_PORTS=(
    $TOOLATHLON_PORT_CANVAS_HTTP
    $TOOLATHLON_PORT_CANVAS_HTTPS
    $TOOLATHLON_PORT_EMAIL_WEB
    $TOOLATHLON_PORT_SMTP
    $TOOLATHLON_PORT_IMAP
    $TOOLATHLON_PORT_SMTP_SUBMISSION
    $TOOLATHLON_PORT_WOOCOMMERCE
    $TOOLATHLON_PORT_K8S_PR_PREVIEW
    $TOOLATHLON_PORT_K8S_MYSQL
    $TOOLATHLON_PORT_MEETING_ASSIGN
)
