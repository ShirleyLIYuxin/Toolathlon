"""
Factory function for creating sandbox executors.

Provides a unified interface to create either Docker or Daytona executors
based on configuration.
"""

import os
import logging
from pathlib import Path
from typing import Optional, Dict, Union

from utils.sandbox.base_executor import BaseSandboxExecutor, SandboxConfig

logger = logging.getLogger(__name__)


def create_executor(
    backend: str = "docker",
    task_dir: str = "",
    model_short_name: str = "",
    provider: str = "unified",
    max_steps: int = 100,
    eval_config: str = "scripts/formal_run_v0.json",
    dump_path: str = "./dumps",
    timeout_sec: int = 1800,
    # Docker-specific
    image_name: str = "lockon0927/toolathlon-task-image:1016beta",
    container_runtime: Optional[str] = None,
    # Daytona-specific
    daytona_target: str = "us",
    daytona_snapshot_name: Optional[str] = None,
    daytona_cpu: int = 4,
    daytona_memory_gb: int = 8,
    daytona_disk_gb: int = 10,
    # Decoupled mode
    runner_mode: str = "containerized",
    agent_framework: str = "toolathlon_default",
    # Daytona project label and decoupled timeouts
    daytona_project_label: str = "toolathlonShirley",
    daytona_gateway_port: int = 10086,
    daytona_gateway_startup_timeout: int = 120,
    daytona_preprocess_timeout: int = 300,
    daytona_eval_timeout: int = 300,
    # Common
    env_vars: Optional[Dict[str, str]] = None,
    project_root: Optional[Path] = None,
    # Config object (alternative to individual params)
    config: Optional[SandboxConfig] = None,
) -> BaseSandboxExecutor:
    """
    Factory function to create a sandbox executor.

    Args:
        backend: Executor backend type ("docker" or "daytona")
        task_dir: Task directory path (e.g., "domain/taskname")
        model_short_name: Model name (e.g., "anthropic/claude-sonnet-4.5")
        provider: Model provider (e.g., "unified")
        max_steps: Maximum steps for the task
        eval_config: Path to evaluation config
        dump_path: Path to save results
        timeout_sec: Task timeout in seconds
        image_name: Docker image name
        container_runtime: Container runtime ("docker" or "podman")
        daytona_target: Daytona target region
        daytona_snapshot_name: Daytona snapshot name (optional)
        daytona_cpu: Daytona CPU allocation
        daytona_memory_gb: Daytona memory allocation in GB
        daytona_disk_gb: Daytona disk allocation in GB
        env_vars: Additional environment variables
        project_root: Project root directory path
        config: Pre-built SandboxConfig (overrides other params if provided)

    Returns:
        A configured sandbox executor instance

    Example:
        >>> executor = create_executor(
        ...     backend="daytona",
        ...     task_dir="debug/debug-task",
        ...     model_short_name="anthropic/claude-sonnet-4.5",
        ... )
        >>> result = await executor.execute_task()
    """
    # Determine container runtime if not specified
    if container_runtime is None:
        container_runtime = _get_container_runtime_from_config(project_root)

    # Build environment variables
    final_env_vars = _build_env_vars(env_vars)

    # Build config if not provided
    if config is None:
        config = SandboxConfig(
            task_dir=task_dir,
            model_short_name=model_short_name,
            provider=provider,
            max_steps=max_steps,
            eval_config=eval_config,
            dump_path=dump_path,
            timeout_sec=timeout_sec,
            image_name=image_name,
            container_runtime=container_runtime,
            daytona_target=daytona_target,
            daytona_snapshot_name=daytona_snapshot_name,
            daytona_cpu=daytona_cpu,
            daytona_memory_gb=daytona_memory_gb,
            daytona_disk_gb=daytona_disk_gb,
            runner_mode=runner_mode,
            agent_framework=agent_framework,
            daytona_project_label=daytona_project_label,
            daytona_gateway_port=daytona_gateway_port,
            daytona_gateway_startup_timeout=daytona_gateway_startup_timeout,
            daytona_preprocess_timeout=daytona_preprocess_timeout,
            daytona_eval_timeout=daytona_eval_timeout,
            env_vars=final_env_vars,
        )

    # Create executor based on backend
    if backend.lower() == "daytona" and config.runner_mode == "decoupled":
        from utils.sandbox.daytona_decoupled_executor import DaytonaDecoupledExecutor
        executor = DaytonaDecoupledExecutor(config)
    elif backend.lower() == "daytona":
        from utils.sandbox.daytona_executor import DaytonaSandboxExecutor
        executor = DaytonaSandboxExecutor(config)
    else:
        from utils.sandbox.docker_executor import DockerSandboxExecutor
        executor = DockerSandboxExecutor(config)

    # Set project root if provided
    if project_root:
        executor.set_project_root(project_root)

    logger.info(f"Created {backend} executor for task: {task_dir}")
    return executor


def create_executor_from_global_config(
    task_dir: str,
    model_short_name: str,
    provider: str = "unified",
    max_steps: int = 100,
    timeout_sec: int = 1800,
    eval_config: str = "scripts/formal_run_v0.json",
    dump_path: str = "./dumps",
    image_name: str = "lockon0927/toolathlon-task-image:1016beta",
    project_root: Optional[Path] = None,
    runner_mode: str = "containerized",
    agent_framework: str = "toolathlon_default",
) -> BaseSandboxExecutor:
    """
    Create an executor using settings from global_configs.

    This reads the sandbox_backend and other settings from
    configs/global_configs.py if available.

    Args:
        task_dir: Task directory path
        model_short_name: Model name
        provider: Model provider
        max_steps: Maximum steps
        timeout_sec: Timeout in seconds
        eval_config: Evaluation config path
        dump_path: Dump path
        image_name: Docker image name
        project_root: Project root path

    Returns:
        Configured sandbox executor
    """
    # Try to load global configs
    config_data = _load_global_configs(project_root)

    # Ensure DAYTONA_API_KEY is set from config (so callers don't need run_parallel.py)
    api_key = config_data.get("daytona_api_key", "")
    if api_key and not os.environ.get("DAYTONA_API_KEY"):
        os.environ["DAYTONA_API_KEY"] = api_key

    # Get backend from config or default to docker
    backend = config_data.get("sandbox_backend", "docker")
    container_runtime = config_data.get("podman_or_docker", "docker")

    # Daytona settings
    daytona_target = config_data.get("daytona_target", "us")
    daytona_snapshot_name = config_data.get("daytona_snapshot_name")

    daytona_resources = config_data.get("daytona_resources", {})
    daytona_cpu = daytona_resources.get("cpu", 4)
    daytona_memory_gb = daytona_resources.get("memory_gb", 8)
    daytona_disk_gb = daytona_resources.get("disk_gb", 10)

    # Daytona project label and decoupled timeouts
    daytona_project_label = config_data.get("daytona_project_label", "toolathlonShirley")
    daytona_timeouts = config_data.get("daytona_timeouts", {})

    return create_executor(
        backend=backend,
        task_dir=task_dir,
        model_short_name=model_short_name,
        provider=provider,
        max_steps=max_steps,
        eval_config=eval_config,
        dump_path=dump_path,
        timeout_sec=timeout_sec,
        image_name=image_name,
        container_runtime=container_runtime,
        daytona_target=daytona_target,
        daytona_snapshot_name=daytona_snapshot_name,
        daytona_cpu=daytona_cpu,
        daytona_memory_gb=daytona_memory_gb,
        daytona_disk_gb=daytona_disk_gb,
        runner_mode=runner_mode,
        agent_framework=agent_framework,
        daytona_project_label=daytona_project_label,
        daytona_gateway_port=daytona_timeouts.get("gateway_port", 10086),
        daytona_gateway_startup_timeout=daytona_timeouts.get("gateway_startup", 120),
        daytona_preprocess_timeout=daytona_timeouts.get("preprocess", 300),
        daytona_eval_timeout=daytona_timeouts.get("eval", 300),
        project_root=project_root,
    )


def _get_container_runtime_from_config(project_root: Optional[Path] = None) -> str:
    """Get container runtime from global config."""
    config_data = _load_global_configs(project_root)
    return config_data.get("podman_or_docker", "docker")


def _load_global_configs(project_root: Optional[Path] = None) -> Dict:
    """Load global configs from the project."""
    try:
        import sys

        if project_root:
            configs_path = project_root / "configs"
            if str(configs_path) not in sys.path:
                sys.path.insert(0, str(configs_path))

        from global_configs import global_configs
        return dict(global_configs)
    except Exception as e:
        logger.debug(f"Could not load global configs: {e}")
        return {}


def _build_env_vars(extra_env_vars: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build environment variables to pass to the sandbox."""
    env_vars = {}

    # Propagate TOOLATHLON environment variables
    env_var_names = [
        "TOOLATHLON_OPENAI_BASE_URL",
        "TOOLATHLON_OPENAI_API_KEY",
        "TOOLATHLON_MODEL_PARAMS_FILE",
    ]

    for name in env_var_names:
        value = os.environ.get(name)
        if value:
            env_vars[name] = value

    # Add extra env vars
    if extra_env_vars:
        env_vars.update(extra_env_vars)

    # Remap model params file path to container-internal path
    # (the actual file is uploaded by daytona_executor._upload_project_files)
    if "TOOLATHLON_MODEL_PARAMS_FILE" in env_vars:
        env_vars["TOOLATHLON_MODEL_PARAMS_FILE"] = "/workspace/model_params.json"

    return env_vars


def is_daytona_available() -> bool:
    """Check if Daytona SDK is available."""
    try:
        import daytona
        return True
    except ImportError:
        return False


def get_available_backends() -> list:
    """Get list of available executor backends."""
    backends = ["docker"]

    if is_daytona_available():
        backends.append("daytona")

    return backends
