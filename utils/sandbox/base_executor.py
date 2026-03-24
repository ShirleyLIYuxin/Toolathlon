"""
Base executor interface for sandbox environments.
Provides abstract base class for Docker and Daytona executors.
Design reference: harbor/src/harbor/environments/base.py
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict


@dataclass
class ExecResult:
    """Result of executing a command in the sandbox."""
    stdout: str
    stderr: str
    return_code: int


@dataclass
class ExecutionResult:
    """Result of executing a complete Toolathlon task."""
    success: bool
    return_code: int
    log_content: str
    eval_passed: Optional[bool] = None
    error: Optional[str] = None
    elapsed_seconds: float = 0.0
    eval_result: Optional[Dict] = None


@dataclass
class SandboxConfig:
    """Configuration for sandbox execution."""
    # Common settings
    task_dir: str  # e.g., "domain/taskname"
    model_short_name: str
    provider: str = "unified"
    max_steps: int = 100
    eval_config: str = "scripts/formal_run_v0.json"
    dump_path: str = "./dumps"
    timeout_sec: int = 1800

    # Docker-specific settings
    image_name: str = "lockon0927/toolathlon-task-image:1016beta"
    container_runtime: str = "docker"  # "docker" or "podman"

    # Daytona-specific settings
    daytona_target: str = "us"
    daytona_snapshot_name: Optional[str] = None
    daytona_cpu: int = 4
    daytona_memory_gb: int = 8
    daytona_disk_gb: int = 20

    # Decoupled mode settings
    runner_mode: str = "containerized"  # "containerized" or "decoupled"
    agent_framework: str = "toolathlon_default"  # "toolathlon_default" or "claude_agent_sdk"

    # Environment variables to pass
    env_vars: Dict[str, str] = field(default_factory=dict)


class BaseSandboxExecutor(ABC):
    """
    Abstract base class for sandbox executors.

    Design reference: harbor/src/harbor/environments/base.py (BaseEnvironment)

    This provides a unified interface for executing Toolathlon tasks
    in different sandbox environments (Docker, Daytona, etc.).
    """

    def __init__(self, config: SandboxConfig):
        """
        Initialize the executor with configuration.

        Args:
            config: SandboxConfig with all necessary settings
        """
        self.config = config
        self._started = False

    @property
    @abstractmethod
    def executor_type(self) -> str:
        """Return the type of executor (e.g., 'docker', 'daytona')."""
        pass

    @abstractmethod
    async def start(self) -> None:
        """
        Start the execution environment.

        For Docker: Start the container
        For Daytona: Create the sandbox
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop and clean up the environment.

        For Docker: Stop and remove the container
        For Daytona: Delete the sandbox
        """
        pass

    @abstractmethod
    async def exec(
        self,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_sec: Optional[int] = None
    ) -> ExecResult:
        """
        Execute a command in the sandbox.

        Args:
            command: The command to execute
            cwd: Working directory for the command
            env: Additional environment variables
            timeout_sec: Command timeout in seconds

        Returns:
            ExecResult with stdout, stderr, and return code
        """
        pass

    @abstractmethod
    async def upload_file(self, source: Path, target: str) -> None:
        """
        Upload a single file to the sandbox.

        Args:
            source: Local file path
            target: Target path in the sandbox
        """
        pass

    @abstractmethod
    async def upload_dir(self, source: Path, target: str) -> None:
        """
        Upload a directory to the sandbox.

        Args:
            source: Local directory path
            target: Target directory path in the sandbox
        """
        pass

    @abstractmethod
    async def download_file(self, source: str, target: Path) -> None:
        """
        Download a single file from the sandbox.

        Args:
            source: File path in the sandbox
            target: Local target path
        """
        pass

    @abstractmethod
    async def download_dir(self, source: str, target: Path) -> None:
        """
        Download a directory from the sandbox.

        Args:
            source: Directory path in the sandbox
            target: Local target directory path
        """
        pass

    async def execute_task(self) -> ExecutionResult:
        """
        Execute a complete Toolathlon task.

        This is the main entry point that:
        1. Starts the sandbox
        2. Uploads project files
        3. Runs main.py
        4. Downloads results
        5. Cleans up

        Returns:
            ExecutionResult with success status and details
        """
        import time
        start_time = time.time()

        try:
            await self.start()
            result = await self._run_task_internal()
            elapsed = time.time() - start_time
            result.elapsed_seconds = elapsed
            return result
        except Exception as e:
            elapsed = time.time() - start_time
            return ExecutionResult(
                success=False,
                return_code=-1,
                log_content="",
                error=str(e),
                elapsed_seconds=elapsed
            )
        finally:
            try:
                await self.stop()
            except Exception:
                pass  # Ignore cleanup errors

    async def _run_task_internal(self) -> ExecutionResult:
        """
        Internal method to run the task after sandbox is started.
        Can be overridden by subclasses for custom behavior.
        """
        # Upload project files
        await self._upload_project_files()

        # Build and run the main command
        cmd = self._build_task_command()
        result = await self.exec(cmd, cwd="/workspace", timeout_sec=self.config.timeout_sec)

        # Try to read evaluation result
        eval_result = await self._read_eval_result()

        return ExecutionResult(
            success=result.return_code == 0,
            return_code=result.return_code,
            log_content=result.stdout + "\n" + result.stderr,
            eval_passed=eval_result.get("pass") if eval_result else None,
            eval_result=eval_result
        )

    async def _upload_project_files(self) -> None:
        """Upload necessary project files to the sandbox."""
        # This is implemented by subclasses based on their specific needs
        pass

    def _build_task_command(self) -> str:
        """Build the command to run main.py."""
        return (
            f"uv run main.py "
            f"--eval_config {self.config.eval_config} "
            f"--task_dir {self.config.task_dir} "
            f"--max_steps_under_single_turn_mode {self.config.max_steps} "
            f"--model_short_name {self.config.model_short_name} "
            f"--provider {self.config.provider} "
            f"--debug"
        )

    async def _read_eval_result(self) -> Optional[Dict]:
        """Try to read the evaluation result from the sandbox."""
        import json
        import tempfile

        task_parts = self.config.task_dir.split("/")
        if len(task_parts) >= 2:
            eval_path = f"/workspace/dumps/{task_parts[0]}/{task_parts[1]}/eval_res.json"
        else:
            eval_path = f"/workspace/dumps/{self.config.task_dir}/eval_res.json"

        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                tmp_path = Path(tmp.name)

            await self.download_file(eval_path, tmp_path)

            with open(tmp_path, "r") as f:
                return json.load(f)
        except Exception:
            return None
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
