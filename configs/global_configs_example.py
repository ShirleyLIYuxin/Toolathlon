# Please fill in the actual content of this file, and copy it, removing the _example suffix
from addict import Dict
global_configs = Dict(
    # these api keys are optional is you use the unified model provider
    aihubmix_key="xxx",
    openrouter_key="xxx",
    qwen_official_key="xxx",
    kimi_official_key="xxx",
    deepseek_official_key="xxx",
    anthropic_official_key="xxx",
    openai_official_key="xxx",
    google_official_key="xxx",
    xai_official_key="xxx",

    # the following two are necessary
    podman_or_docker="docker", # or `podman` depending on which one you want to use
    notion_preprocess_with_playwright=False, # In genral you do not need to change this! It is whether you use mcp/playwright to preprocess the notion page, default as false.

    # ============================================================
    # Sandbox Backend Configuration (Daytona Integration)
    # ============================================================
    # Choose execution backend: "docker" (default) or "daytona"
    sandbox_backend="docker",

    # Daytona-specific configuration (only used when sandbox_backend="daytona")
    # API key can also be set via DAYTONA_API_KEY environment variable
    daytona_api_key="",
    daytona_target="us",  # Daytona target region
    daytona_snapshot_name="",  # Optional: use a snapshot for faster startup (e.g., "toolathlon-base")

    # Daytona resource allocation
    daytona_resources=Dict(
        cpu=4,        # Number of CPUs
        memory_gb=8,  # Memory in GB
        disk_gb=20,   # Disk space in GB
    ),

    # Daytona project label — used to tag sandboxes for safe identification/cleanup.
    # Change this if sharing a Daytona account with others.
    daytona_project_label="toolathlonShirley",

    # Daytona decoupled mode overrides (only used when runner_mode="decoupled")
    daytona_timeouts=Dict(
        gateway_port=10086,          # SSE gateway port inside sandbox
        gateway_startup=120,         # Seconds to wait for gateway readiness
        preprocess=300,              # Seconds for preprocess phase
        eval=300,                    # Seconds for evaluation phase
    ),
)