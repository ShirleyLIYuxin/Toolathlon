# record start time
start_time=$(date +%s)
echo "Start time: $(date)"

# Load instance configuration (ports, instance suffix, etc.)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../configs/load_instance_env.sh"

poste_configure_dovecot=${1:-true}
echo "============================================================================================="
echo "poste_configure_dovecot: $poste_configure_dovecot"
echo "For some Linux distributions, you need to configure Dovecot to allow plaintext auth."
echo "If you are not sure, please set to true."
echo "Our experience: Ubuntu 24.04 should set this as true, but AlmaLinux should set this as false."
echo "============================================================================================="
echo "Instance config: suffix='$TOOLATHLON_INSTANCE_SUFFIX' prefix='$TOOLATHLON_INSTANCE_PREFIX'"
echo "============================================================================================="

sleep 5

# Kill processes occupying required ports
echo "============================================================================================="
echo "Checking and killing processes on required ports..."
echo "============================================================================================="

# Use ports from instance config
REQUIRED_PORTS=("${TOOLATHLON_REQUIRED_PORTS[@]}")

for port in "${REQUIRED_PORTS[@]}"; do
    # Check if port is in use
    if lsof -i :$port -t >/dev/null 2>&1; then
        echo "Port $port is in use. Killing process(es)..."
        # Get PIDs and kill them
        pids=$(lsof -i :$port -t)
        for pid in $pids; do
            echo "  Killing PID $pid on port $port"
            kill -9 $pid 2>/dev/null || true
        done
        # Wait a moment for the port to be released
        sleep 1
        echo "  Port $port cleared"
    else
        echo "Port $port is free"
    fi
done

echo "All required ports checked and cleared"
echo "============================================================================================="
echo ""

# this is just to launch a test cluster (also clear existing ones) to make sure the MCP servers are ready to use
bash deployment/k8s/scripts/setup.sh # this is to create one test cluster

# Pass ports from instance config to deployment scripts
bash deployment/canvas/scripts/setup.sh "" "" "" $TOOLATHLON_PORT_CANVAS_HTTP $TOOLATHLON_PORT_CANVAS_HTTPS "$TOOLATHLON_INSTANCE_SUFFIX"

bash deployment/poste/scripts/setup.sh start $poste_configure_dovecot $TOOLATHLON_PORT_EMAIL_WEB $TOOLATHLON_PORT_SMTP $TOOLATHLON_PORT_IMAP $TOOLATHLON_PORT_SMTP_SUBMISSION "$TOOLATHLON_INSTANCE_SUFFIX"

bash deployment/woocommerce/scripts/setup.sh start 81 20 $TOOLATHLON_PORT_WOOCOMMERCE "$TOOLATHLON_INSTANCE_SUFFIX"

# we also use 30123, 30124 ports in two of the k8s tasks

# we also use 30137 for a web task to deploy a web page locally

# record exit time
echo "Exit time: $(date)"

# record total time
echo "Total time: $(($(date +%s) - start_time)) seconds"