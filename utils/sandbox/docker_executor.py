"""
Docker sandbox executor for Toolathlon.

Encapsulates the existing run_single_containerized.sh logic in a Python class
that implements the BaseSandboxExecutor interface.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

from utils.sandbox.base_executor import BaseSandboxExecutor, ExecResult, SandboxConfig

logger = logging.getLogger(__name__)


class DockerSandboxExecutor(BaseSandboxExecutor):
    """
    Docker/Podman sandbox executor for running Toolathlon tasks.

    This wraps the existing containerized execution logic in a class
    that matches the BaseSandboxExecutor interface.
    """

    def __init__(self, config: SandboxConfig):
        """
        Initialize the Docker executor.

        Args:
            config: SandboxConfig with Docker-specific settings
        """
        super().__init__(config)
        self._container_name: Optional[str] = None
        self._container_id: Optional[str] = None
        self._project_root: Optional[Path] = None
        self._runtime = config.container_runtime

    @property
    def executor_type(self) -> str:
        return "docker"

    def set_project_root(self, path: Path) -> None:
        """Set the project root directory for file operations."""
        self._project_root = path

    def _generate_container_name(self) -> str:
        """Generate a unique container name."""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_task_name = self.config.task_dir.replace("/", "-")

        # Try to read instance prefix from config
        instance_prefix = ""
        if self._project_root:
            try:
                import yaml
                ports_config_path = self._project_root / "configs" / "ports_config.yaml"
                if ports_config_path.exists():
                    with open(ports_config_path, "r") as f:
                        config_data = yaml.safe_load(f)
                        instance_prefix = config_data.get("instance_prefix", "")
            except Exception:
                pass

        return f"{instance_prefix}toolathlon-{safe_task_name}-{timestamp}"

    def _run_command(self, cmd: list, capture_output: bool = True) -> subprocess.CompletedProcess:
        """Run a shell command."""
        return subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
        )

    async def _run_command_async(self, cmd: list) -> tuple[int, str, str]:
        """Run a shell command asynchronously."""
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode or 0, stdout.decode(), stderr.decode()

    async def start(self) -> None:
        """Start the Docker container."""
        if self._started:
            logger.warning("Container already started")
            return

        if not self._project_root:
            self._project_root = Path.cwd()

        self._container_name = self._generate_container_name()

        # Prepare container arguments
        runtime = self._runtime
        image_name = self.config.image_name

        # Create output directories
        task_parts = self.config.task_dir.split("/")
        if len(task_parts) >= 2:
            output_folder = Path(self.config.dump_path) / task_parts[0] / task_parts[1]
        else:
            output_folder = Path(self.config.dump_path) / self.config.task_dir

        output_folder.mkdir(parents=True, exist_ok=True)
        log_dir = output_folder
        log_dir.mkdir(parents=True, exist_ok=True)

        output_folder_abs = output_folder.resolve()
        log_dir_abs = log_dir.resolve()

        # Build container run command
        cmd = [
            runtime, "run",
            "-d",  # Detached mode
            "--name", self._container_name,
            "--network", "host",
        ]

        # Add environment variables
        for key, value in self.config.env_vars.items():
            cmd.extend(["-e", f"{key}={value}"])

        # Add socket mount based on runtime
        if runtime == "podman":
            # Check for podman socket
            if os.path.exists("/run/podman/podman.sock"):
                cmd.extend(["-v", "/run/podman/podman.sock:/run/podman/podman.sock"])
            elif os.path.exists(f"/run/user/{os.getuid()}/podman/podman.sock"):
                cmd.extend(["-v", f"/run/user/{os.getuid()}/podman/podman.sock:/run/podman/podman.sock"])
            cmd.extend(["-e", "KIND_EXPERIMENTAL_PROVIDER=podman"])
        else:
            cmd.extend(["-v", "/var/run/docker.sock:/var/run/docker.sock"])

        # Add volume mounts
        cmd.extend([
            "-v", f"{output_folder_abs}:/workspace/dumps",
            "-v", f"{log_dir_abs}:/workspace/logs",
            "-w", "/workspace",
            image_name,
            "sleep", "3600",  # Keep container alive
        ])

        # Start container
        logger.info(f"Starting container: {self._container_name}")
        result = self._run_command(cmd)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to start container: {result.stderr}")

        self._container_id = result.stdout.strip()

        # Wait for container to be ready
        max_wait = 30
        for i in range(max_wait):
            check_cmd = [runtime, "exec", self._container_name, "echo", "ready"]
            check_result = self._run_command(check_cmd)
            if check_result.returncode == 0:
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError(f"Container not ready after {max_wait} seconds")

        self._started = True
        logger.info(f"Container started: {self._container_name}")

    async def stop(self) -> None:
        """Stop and remove the Docker container."""
        if not self._started or not self._container_name:
            return

        runtime = self._runtime

        try:
            # Stop container
            self._run_command([runtime, "stop", self._container_name], capture_output=True)
            # Remove container
            self._run_command([runtime, "rm", self._container_name], capture_output=True)
            logger.info(f"Container removed: {self._container_name}")
        except Exception as e:
            logger.warning(f"Error removing container: {e}")
        finally:
            self._container_name = None
            self._container_id = None
            self._started = False

    async def exec(
        self,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_sec: Optional[int] = None
    ) -> ExecResult:
        """Execute a command in the container."""
        if not self._started or not self._container_name:
            raise RuntimeError("Container not started")

        runtime = self._runtime

        # Build exec command
        cmd = [runtime, "exec"]

        # Add environment variables
        cmd.extend(["--env", "DOCKER_API_VERSION=1.44"])

        if env:
            for key, value in env.items():
                cmd.extend(["--env", f"{key}={value}"])

        cmd.append(self._container_name)

        # Build shell command
        shell_cmd = command
        if cwd:
            shell_cmd = f"cd {cwd} && {shell_cmd}"

        cmd.extend(["bash", "-c", shell_cmd])

        # Execute with timeout
        try:
            if timeout_sec:
                returncode, stdout, stderr = await asyncio.wait_for(
                    self._run_command_async(cmd),
                    timeout=timeout_sec
                )
            else:
                returncode, stdout, stderr = await self._run_command_async(cmd)

            return ExecResult(
                stdout=stdout,
                stderr=stderr,
                return_code=returncode,
            )
        except asyncio.TimeoutError:
            return ExecResult(
                stdout="",
                stderr=f"Command timed out after {timeout_sec} seconds",
                return_code=-1,
            )

    async def upload_file(self, source: Path, target: str) -> None:
        """Upload a file to the container."""
        if not self._started or not self._container_name:
            raise RuntimeError("Container not started")

        runtime = self._runtime
        cmd = [runtime, "cp", str(source), f"{self._container_name}:{target}"]
        result = self._run_command(cmd)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to upload file: {result.stderr}")

    async def upload_dir(self, source: Path, target: str) -> None:
        """Upload a directory to the container."""
        if not self._started or not self._container_name:
            raise RuntimeError("Container not started")

        runtime = self._runtime

        # Ensure target directory exists
        await self.exec(f"mkdir -p {target}")

        # Docker cp copies the directory contents
        cmd = [runtime, "cp", str(source), f"{self._container_name}:{target}"]
        result = self._run_command(cmd)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to upload directory: {result.stderr}")

    async def download_file(self, source: str, target: Path) -> None:
        """Download a file from the container."""
        if not self._started or not self._container_name:
            raise RuntimeError("Container not started")

        runtime = self._runtime
        target.parent.mkdir(parents=True, exist_ok=True)
        cmd = [runtime, "cp", f"{self._container_name}:{source}", str(target)]
        result = self._run_command(cmd)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to download file: {result.stderr}")

    async def download_dir(self, source: str, target: Path) -> None:
        """Download a directory from the container."""
        if not self._started or not self._container_name:
            raise RuntimeError("Container not started")

        runtime = self._runtime
        target.mkdir(parents=True, exist_ok=True)
        cmd = [runtime, "cp", f"{self._container_name}:{source}/.", str(target)]
        result = self._run_command(cmd)

        if result.returncode != 0:
            # Try without /. suffix for compatibility
            cmd = [runtime, "cp", f"{self._container_name}:{source}", str(target)]
            result = self._run_command(cmd)
            if result.returncode != 0:
                raise RuntimeError(f"Failed to download directory: {result.stderr}")

    async def _upload_project_files(self) -> None:
        """Upload necessary project files to the container."""
        if not self._project_root:
            self._project_root = Path.cwd()

        project_root = self._project_root
        runtime = self._runtime

        # Create directory structure
        await self.exec("mkdir -p /workspace/deployment /workspace/deployment/canvas /workspace/global_preparation /workspace/tasks")

        # Files to copy (matching run_single_containerized.sh)
        items_to_copy = [
            "configs",
            "scripts",
            "utils",
            "main.py",
        ]

        optional_items = [
            "deployment/k8s",
            "deployment/canvas/logs",
            "global_preparation/check_installation.py",
            "local_binary/github-mcp-server",
        ]

        # Copy items
        for item in items_to_copy:
            source = project_root / item
            if source.exists():
                if source.is_dir():
                    await self.upload_dir(source, f"/workspace/{item}")
                else:
                    await self.upload_file(source, f"/workspace/{item}")
                logger.debug(f"Uploaded {item}")

        for item in optional_items:
            source = project_root / item
            if source.exists():
                parent = os.path.dirname(item)
                if parent:
                    await self.exec(f"mkdir -p /workspace/{parent}")
                if source.is_dir():
                    await self.upload_dir(source, f"/workspace/{item}")
                else:
                    await self.upload_file(source, f"/workspace/{item}")
                logger.debug(f"Uploaded optional {item}")

        # Copy task directory
        task_parts = self.config.task_dir.split("/")
        if len(task_parts) >= 2:
            task_parent = task_parts[0]
            task_source = project_root / "tasks" / self.config.task_dir
        else:
            task_parent = ""
            task_source = project_root / "tasks" / self.config.task_dir

        if task_source.exists():
            if task_parent:
                await self.exec(f"mkdir -p /workspace/tasks/{task_parent}")
            await self.upload_dir(task_source, f"/workspace/tasks/{self.config.task_dir}")
            logger.debug(f"Uploaded task: {self.config.task_dir}")

        # Setup MCP configs
        await self._setup_mcp_configs()

    async def _setup_mcp_configs(self) -> None:
        """Setup MCP configurations in the container."""
        if not self._project_root:
            return

        project_root = self._project_root

        # Copy GCP OAuth configs
        copy_config_cmd = """
        for dir in ~/.gmail-mcp ~/.calendar-mcp; do
            mkdir -p $dir
            cp ./configs/gcp-oauth.keys.json $dir/ 2>/dev/null || true
            cp ./configs/google_credentials.json $dir/credentials.json 2>/dev/null || true
        done
        """
        await self.exec(copy_config_cmd, cwd="/workspace")

        # Copy MCP auth directory
        mcp_auth_source = project_root / "configs" / ".mcp-auth"
        if mcp_auth_source.exists():
            await self.exec("mkdir -p /root/.mcp-auth")
            await self.upload_dir(mcp_auth_source, "/root/.mcp-auth")
        else:
            home_mcp_auth = Path.home() / ".mcp-auth"
            if home_mcp_auth.exists():
                await self.exec("mkdir -p /root/.mcp-auth")
                await self.upload_dir(home_mcp_auth, "/root/.mcp-auth")

    async def download_results(self, local_dump_path: Path) -> None:
        """
        Download task results from the container to local dump path.

        Args:
            local_dump_path: Local directory to save results
        """
        # Docker volume-mounts $output_folder/$domain/$task → /workspace/dumps,
        # so /workspace/dumps is already the task-specific folder (flat, not nested)
        container_result_path = "/workspace/dumps"

        task_parts = self.config.task_dir.split("/")
        if len(task_parts) >= 2:
            local_result_path = local_dump_path / task_parts[0] / task_parts[1]
        else:
            local_result_path = local_dump_path / self.config.task_dir

        local_result_path.mkdir(parents=True, exist_ok=True)

        try:
            await self.download_dir(container_result_path, local_result_path)
            logger.info(f"Results downloaded to: {local_result_path}")
        except Exception as e:
            logger.warning(f"Failed to download results: {e}")
