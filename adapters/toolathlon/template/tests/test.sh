#!/bin/bash
# Template test script for Toolathlon tasks
# This will be customized per-task by the adapter

set -e

cd /workspace

# Run Python evaluation
python evaluation/main.py \
    --agent_workspace /app \
    --groundtruth_workspace /workspace/groundtruth_workspace \
    --res_log_file /logs/verifier/evaluation.log

exit_code=$?

# Write Harbor reward
mkdir -p /logs/verifier
if [ $exit_code -eq 0 ]; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi

exit $exit_code
