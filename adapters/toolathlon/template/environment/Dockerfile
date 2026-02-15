# Template Dockerfile for Toolathlon tasks
# This extends the pre-built Toolathlon base image

FROM ghcr.io/toolathlon/toolathlon-base:latest

# Task-specific configuration will be added here by the adapter
WORKDIR /app

# Create necessary directories
RUN mkdir -p /logs/verifier /logs/agent /workspace

# Copy task files (these will be populated by the adapter)
# COPY tests/evaluation /workspace/evaluation
# COPY tests/groundtruth /workspace/groundtruth_workspace
# COPY initial_workspace/ /app/

CMD ["/bin/bash"]
