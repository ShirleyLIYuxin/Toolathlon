"""
Daytona sandbox executor for Toolathlon.

Design reference: harbor/src/harbor/environments/daytona.py (DaytonaEnvironment)

This executor runs Toolathlon tasks in Daytona cloud sandbox environments,
providing isolation and reproducibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Optional, Dict, Any, TYPE_CHECKING
from uuid import uuid4

try:
    from daytona import (
        AsyncDaytona,
        AsyncSandbox,
        CreateSandboxFromImageParams,
        CreateSandboxFromSnapshotParams,
        FileDownloadRequest,
        FileUpload,
        Image,
        Resources,
        SessionExecuteRequest,
    )
    from daytona._async.snapshot import SnapshotState
    DAYTONA_AVAILABLE = True
except ImportError:
    DAYTONA_AVAILABLE = False
    # Define placeholder types for type checking when daytona is not installed
    AsyncDaytona = None
    AsyncSandbox = None
    CreateSandboxFromImageParams = Any
    CreateSandboxFromSnapshotParams = Any
    FileDownloadRequest = None
    FileUpload = None
    Image = None
    Resources = None
    SessionExecuteRequest = None
    SnapshotState = None

from tenacity import retry, stop_after_attempt, wait_exponential

from utils.sandbox.base_executor import BaseSandboxExecutor, ExecResult, SandboxConfig
from utils.sandbox.daytona_client_manager import DaytonaClientManager

logger = logging.getLogger(__name__)


class DaytonaSandboxExecutor(BaseSandboxExecutor):
    """
    Daytona sandbox executor for running Toolathlon tasks.

    Key features:
    1. Creates sandbox from snapshot or Docker image
    2. Uploads Toolathlon project files
    3. Executes main.py in the sandbox
    4. Downloads logs and results
    5. Cleans up sandbox on completion
    """

    def __init__(self, config: SandboxConfig):
        """
        Initialize the Daytona executor.

        Args:
            config: SandboxConfig with Daytona-specific settings
        """
        if not DAYTONA_AVAILABLE:
            raise ImportError(
                "daytona package is not installed. "
                "Please install it with: pip install daytona"
            )

        super().__init__(config)
        self._sandbox: Optional[AsyncSandbox] = None
        self._client_manager: Optional[DaytonaClientManager] = None
        self._project_root: Optional[Path] = None

    @property
    def executor_type(self) -> str:
        return "daytona"

    def set_project_root(self, path: Path) -> None:
        """Set the project root directory for file uploads."""
        self._project_root = path

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _create_sandbox(
        self, params: CreateSandboxFromImageParams | CreateSandboxFromSnapshotParams
    ):
        """Create a sandbox with retry logic."""
        if not self._client_manager:
            raise RuntimeError("Client manager not initialized")

        daytona = await self._client_manager.get_client()
        self._sandbox = await daytona.create(
            params=params,
            timeout=self.config.timeout_sec
        )

    async def start(self) -> None:
        """
        Start the Daytona sandbox.

        Creates a sandbox using one of the following methods (in order of preference):
        1. From snapshot if snapshot_name is specified and exists
        2. From Docker image
        """
        if self._started:
            logger.warning("Sandbox already started")
            return

        # Build resources specification
        resources = Resources(
            cpu=self.config.daytona_cpu,
            memory=self.config.daytona_memory_gb,
            disk=self.config.daytona_disk_gb,
        )

        # Get client manager
        self._client_manager = await DaytonaClientManager.get_instance()
        daytona = await self._client_manager.get_client()

        # Check for snapshot
        snapshot_exists = False
        if self.config.daytona_snapshot_name:
            try:
                snapshot = await daytona.snapshot.get(self.config.daytona_snapshot_name)
                if snapshot.state == SnapshotState.ACTIVE:
                    snapshot_exists = True
                    logger.debug(f"Found active snapshot: {self.config.daytona_snapshot_name}")
            except Exception as e:
                logger.debug(f"Snapshot not found: {e}")
                snapshot_exists = False

        # Labels for sandbox identification (prevents accidental deletion of other projects)
        labels = {
            "project": "toolathlonShirley",
            "task": self.config.task_dir,
        }

        # Create sandbox
        if snapshot_exists and self.config.daytona_snapshot_name:
            logger.info(f"Creating sandbox from snapshot: {self.config.daytona_snapshot_name}")
            params = CreateSandboxFromSnapshotParams(
                auto_delete_interval=0,
                auto_stop_interval=0,
                snapshot=self.config.daytona_snapshot_name,
                labels=labels,
            )
        else:
            logger.info(f"Creating sandbox from image: {self.config.image_name}")
            image = Image.base(self.config.image_name)
            params = CreateSandboxFromImageParams(
                image=image,
                auto_delete_interval=0,
                auto_stop_interval=0,
                resources=resources,
                labels=labels,
            )

        await self._create_sandbox(params=params)

        # Create workspace directory structure (matching Docker executor's mkdir)
        await self.exec(
            "mkdir -p /workspace/dumps /workspace/logs /workspace/tasks "
            "/workspace/deployment /workspace/deployment/canvas /workspace/global_preparation"
        )

        # Persist env vars so they're visible to all processes.
        # Docker sets env vars at container level via `docker run -e`;
        # Daytona has no equivalent, so we write to /etc/profile.d/.
        # bash -lc (login shell) sources /etc/profile → /etc/profile.d/*.sh
        # (NOT ~/.bashrc, which is only for interactive non-login shells)
        if self.config.env_vars:
            export_lines = []
            for key, value in self.config.env_vars.items():
                escaped_value = value.replace("'", "'\\''")
                export_lines.append(f"export {key}='{escaped_value}'")
            # Write to /etc/profile.d/ for login shells (bash -lc)
            profile_content = "\n".join(export_lines)
            await self.exec(
                f"echo {shlex.quote(profile_content)} > /etc/profile.d/toolathlon-env.sh"
            )
            # Also set immediately in current exec context for setup commands
            export_cmd = " && ".join(export_lines)
            await self.exec(export_cmd)

        self._started = True
        logger.info(f"Sandbox started: {self._sandbox.id if self._sandbox else 'unknown'}")

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _delete_sandbox(self):
        """Delete sandbox with retry logic."""
        if self._sandbox:
            await self._sandbox.delete()

    async def stop(self) -> None:
        """Stop and delete the Daytona sandbox."""
        if not self._started:
            return

        try:
            if self._sandbox:
                logger.info(f"Deleting sandbox: {self._sandbox.id}")
                await self._delete_sandbox()
                logger.info("Sandbox deleted successfully")
        except Exception as e:
            logger.error(f"Error deleting sandbox: {e}")
        finally:
            self._sandbox = None
            self._client_manager = None
            self._started = False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _get_session_command_with_retry(self, session_id: str, command_id: str):
        """Get session command status with retry."""
        if not self._sandbox:
            raise RuntimeError("Sandbox not started")
        return await self._sandbox.process.get_session_command(session_id, command_id)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _get_session_command_logs_with_retry(self, session_id: str, command_id: str):
        """Get session command logs with retry."""
        if not self._sandbox:
            raise RuntimeError("Sandbox not started")
        return await self._sandbox.process.get_session_command_logs(session_id, command_id)

    async def _poll_response(self, session_id: str, command_id: str) -> ExecResult:
        """Poll for command completion and return result."""
        if not self._sandbox:
            raise RuntimeError("Sandbox not started")

        response = await self._get_session_command_with_retry(session_id, command_id)

        while response.exit_code is None:
            await asyncio.sleep(1)
            response = await self._get_session_command_with_retry(session_id, response.id)

        logs = await self._get_session_command_logs_with_retry(session_id, command_id)

        return ExecResult(
            stdout=logs.stdout or "",
            stderr=logs.stderr or "",
            return_code=int(response.exit_code),
        )

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
            cwd: Working directory
            env: Additional environment variables
            timeout_sec: Command timeout

        Returns:
            ExecResult with stdout, stderr, and return code
        """
        if not self._sandbox:
            raise RuntimeError("Sandbox not started. Call start() first.")

        session_id = str(uuid4())

        try:
            await self._sandbox.process.create_session(session_id)

            # Build command with bash wrapper (login shell, matching Harbor pattern)
            full_command = f"bash -lc {shlex.quote(command)}"

            # Order follows Harbor's daytona.py: env vars → timeout → cwd
            # 1. Env vars closest to command (visible to bash -lc)
            if env:
                for key, value in env.items():
                    full_command = f"{key}={shlex.quote(value)} {full_command}"

            # config.env_vars 已在 start() 中持久化到 .bashrc，
            # bash -lc 会自动加载，无需每次命令重复注入

            # 2. Timeout wraps everything (can kill the whole command chain)
            if timeout_sec:
                full_command = f"timeout {timeout_sec} {full_command}"

            # 3. Working directory
            if cwd:
                full_command = f"cd {cwd} && {full_command}"

            response = await self._sandbox.process.execute_session_command(
                session_id,
                SessionExecuteRequest(
                    command=full_command,
                    run_async=True,
                ),
                timeout=timeout_sec,
            )

            if response.cmd_id is None:
                raise RuntimeError("Cannot find command ID")

            result = await self._poll_response(session_id, response.cmd_id)
            return result

        finally:
            # Don't delete session - Daytona will delete any child processes
            pass

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def upload_file(self, source: Path, target: str) -> None:
        """Upload a single file to the sandbox."""
        if not self._sandbox:
            raise RuntimeError("Sandbox not started")

        await self._sandbox.fs.upload_file(str(source), target)

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def upload_dir(self, source: Path, target: str) -> None:
        """Upload a directory to the sandbox."""
        if not self._sandbox:
            raise RuntimeError("Sandbox not started")

        file_uploads = []
        source = Path(source)

        for file_path in source.rglob("*"):
            if file_path.is_file():
                relative_path = file_path.relative_to(source)
                destination_path = str(Path(target) / relative_path)

                file_uploads.append(
                    FileUpload(
                        source=str(file_path),
                        destination=destination_path,
                    )
                )

        if file_uploads:
            await self._sandbox.fs.upload_files(files=file_uploads)

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def download_file(self, source: str, target: Path) -> None:
        """Download a single file from the sandbox."""
        if not self._sandbox:
            raise RuntimeError("Sandbox not started")

        await self._sandbox.fs.download_file(source, str(target))

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def download_dir(self, source: str, target: Path) -> None:
        """Download a directory from the sandbox."""
        if not self._sandbox:
            raise RuntimeError("Sandbox not started")

        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)

        search_result = await self._sandbox.fs.search_files(source, "*")

        file_downloads = []
        files_list = search_result.files if search_result.files else []
        for file_path in files_list:
            file_info = await self._sandbox.fs.get_file_info(file_path)

            if not file_info.is_dir:
                path_obj = Path(file_path)
                relative_path = path_obj.relative_to(Path(source))
                local_file_path = target / relative_path

                local_file_path.parent.mkdir(parents=True, exist_ok=True)

                file_downloads.append(
                    FileDownloadRequest(
                        source=file_path,
                        destination=str(local_file_path),
                    )
                )

        if file_downloads:
            await self._sandbox.fs.download_files(files=file_downloads)

    async def _upload_project_files(self) -> None:
        """Upload necessary project files to the sandbox."""
        if not self._project_root:
            # Try to determine project root from current directory
            self._project_root = Path.cwd()

        project_root = self._project_root

        # Files and directories to upload (matching run_single_containerized.sh)
        items_to_upload = [
            "configs",
            "scripts",
            "utils",
            "main.py",
        ]

        # Optional items (may not exist in all setups)
        optional_items = [
            "deployment/k8s",
            "deployment/canvas/logs",
            "global_preparation/check_installation.py",
            "local_binary/github-mcp-server",
        ]

        # Upload required items
        for item in items_to_upload:
            source = project_root / item
            if source.exists():
                target = f"/workspace/{item}"
                if source.is_dir():
                    await self.upload_dir(source, target)
                else:
                    await self.upload_file(source, target)
                logger.debug(f"Uploaded {item}")
            else:
                logger.warning(f"Required item not found: {item}")

        # Upload optional items
        for item in optional_items:
            source = project_root / item
            if source.exists():
                target = f"/workspace/{item}"
                # Ensure parent directory exists
                await self.exec(f"mkdir -p {os.path.dirname(target)}")
                if source.is_dir():
                    await self.upload_dir(source, target)
                else:
                    await self.upload_file(source, target)
                    # Ensure binary files are executable
                    if "local_binary" in item:
                        await self.exec(f"chmod +x {target}")
                logger.debug(f"Uploaded optional {item}")

        # Upload task directory
        task_parts = self.config.task_dir.split("/")
        if len(task_parts) >= 2:
            task_domain = task_parts[0]
            task_name = task_parts[1]
            task_source = project_root / "tasks" / task_domain / task_name
        else:
            task_source = project_root / "tasks" / self.config.task_dir

        if task_source.exists():
            task_target = f"/workspace/tasks/{self.config.task_dir}"
            await self.exec(f"mkdir -p {os.path.dirname(task_target)}")
            await self.upload_dir(task_source, task_target)
            logger.debug(f"Uploaded task: {self.config.task_dir}")
        else:
            raise FileNotFoundError(f"Task directory not found: {task_source}")

        # Upload model params file if specified (matching shell script behavior)
        model_params_file = os.environ.get("TOOLATHLON_MODEL_PARAMS_FILE")
        if model_params_file and Path(model_params_file).exists():
            await self.upload_file(Path(model_params_file), "/workspace/model_params.json")
            logger.debug(f"Uploaded model params: {model_params_file}")

        # Setup Gmail/Calendar MCP config (if exists)
        await self._setup_mcp_configs()

    async def _setup_mcp_configs(self) -> None:
        """Setup MCP server configurations in the sandbox.

        Matches Docker executor behavior:
        - Best-effort copy of GCP OAuth files (each file independent, skip on missing)
        - MCP auth directory with fallback to ~/.mcp-auth on host
        """
        if not self._project_root:
            return

        project_root = self._project_root

        # Copy GCP OAuth keys — best-effort, each file independent
        # (Docker uses: cp file $dir/ 2>/dev/null || true)
        gcp_oauth_path = project_root / "configs" / "gcp-oauth.keys.json"
        google_creds_path = project_root / "configs" / "google_credentials.json"

        await self.exec("mkdir -p /root/.gmail-mcp /root/.calendar-mcp")

        for target_dir in ["/root/.gmail-mcp", "/root/.calendar-mcp"]:
            if gcp_oauth_path.exists():
                try:
                    await self.upload_file(gcp_oauth_path, f"{target_dir}/gcp-oauth.keys.json")
                except Exception as e:
                    logger.debug(f"Failed to upload gcp-oauth to {target_dir}: {e}")
            if google_creds_path.exists():
                try:
                    await self.upload_file(google_creds_path, f"{target_dir}/credentials.json")
                except Exception as e:
                    logger.debug(f"Failed to upload google_credentials to {target_dir}: {e}")

        # Copy MCP auth directory — with fallback to host ~/.mcp-auth
        # (matching Docker executor's fallback logic)
        mcp_auth_path = project_root / "configs" / ".mcp-auth"
        if not mcp_auth_path.exists():
            mcp_auth_path = Path.home() / ".mcp-auth"

        if mcp_auth_path.exists():
            try:
                await self.exec("mkdir -p /root/.mcp-auth")
                await self.upload_dir(mcp_auth_path, "/root/.mcp-auth")
                logger.debug(f"MCP auth directory uploaded from: {mcp_auth_path}")
                # Cross-copy token files between mcp-remote version directories.
                # Host may generate tokens under e.g. mcp-remote-0.1.37/ but the
                # sandbox may have mcp-remote-0.1.16 installed, which looks in its
                # own version directory.  We find the host version with tokens and
                # upload them directly to the sandbox's installed version directory,
                # avoiding shell quoting issues entirely.
                try:
                    # Find host version dir that has token files
                    host_token_dir = None
                    for d in mcp_auth_path.iterdir():
                        if d.is_dir() and d.name.startswith("mcp-remote-"):
                            if any(f.name.endswith("_tokens.json") for f in d.iterdir()):
                                host_token_dir = d
                                break
                    if host_token_dir:
                        # Get installed mcp-remote version in the sandbox
                        ver_result = await self.exec(
                            "cat /workspace/node_modules/mcp-remote/package.json 2>/dev/null"
                        )
                        if ver_result.stdout and ver_result.return_code == 0:
                            import json as _json
                            installed_ver = _json.loads(ver_result.stdout).get("version", "")
                            installed_dir_name = f"mcp-remote-{installed_ver}"
                            if installed_dir_name != host_token_dir.name:
                                logger.info(
                                    f"mcp-remote version mismatch: host={host_token_dir.name}, "
                                    f"sandbox={installed_dir_name}. Uploading tokens."
                                )
                                for auth_root in ["/workspace/configs/.mcp-auth", "/root/.mcp-auth"]:
                                    target = f"{auth_root}/{installed_dir_name}"
                                    await self.exec(f"mkdir -p {target}")
                                    await self.upload_dir(host_token_dir, target)
                                logger.debug("Token files synced to installed version directory")
                except Exception as e:
                    logger.debug(f"mcp-remote version sync skipped: {e}")
            except Exception as e:
                logger.warning(f"Failed to upload MCP auth: {e}")

    async def _run_task_internal(self):
        """
        Override base class to download results before sandbox is stopped.
        execute_task() calls stop() in its finally block, so we must download here.
        """
        from utils.sandbox.base_executor import ExecutionResult

        # Upload project files
        await self._upload_project_files()

        # Build and run the main command
        cmd = self._build_task_command()
        result = await self.exec(cmd, cwd="/workspace", timeout_sec=self.config.timeout_sec)

        # Download all results to local dump_path BEFORE sandbox is stopped
        try:
            await self.download_results(Path(self.config.dump_path))
        except Exception as e:
            logger.warning(f"Failed to download results in _run_task_internal: {e}")

        # Read eval result from local (already downloaded)
        eval_result = self._read_eval_result_from_local()

        return ExecutionResult(
            success=result.return_code == 0,
            return_code=result.return_code,
            log_content=result.stdout + "\n" + result.stderr,
            eval_passed=eval_result.get("pass") if eval_result else None,
            eval_result=eval_result,
        )

    def _read_eval_result_from_local(self):
        """Read eval result from locally downloaded files."""
        task_parts = self.config.task_dir.split("/")
        if len(task_parts) >= 2:
            eval_path = Path(self.config.dump_path) / task_parts[0] / task_parts[1] / "eval_res.json"
        else:
            eval_path = Path(self.config.dump_path) / self.config.task_dir / "eval_res.json"

        try:
            with open(eval_path, "r") as f:
                return json.load(f)
        except Exception:
            return None

    async def download_results(self, local_dump_path: Path) -> None:
        """
        Download task results from the sandbox to local dump path.

        The eval config uses direct_to_dumps=true, so results are stored
        at /workspace/dumps/ directly (not nested under task folder).
        We download them to local_dump_path/tasks_folder/task_name/.
        """
        # Sandbox stores results flat in /workspace/dumps/
        sandbox_result_path = "/workspace/dumps"

        # Local path includes task folder hierarchy
        task_parts = self.config.task_dir.split("/")
        if len(task_parts) >= 2:
            local_result_path = local_dump_path / task_parts[0] / task_parts[1]
        else:
            local_result_path = local_dump_path / self.config.task_dir

        local_result_path.mkdir(parents=True, exist_ok=True)

        try:
            await self.download_dir(sandbox_result_path, local_result_path)
            logger.info(f"Results downloaded to: {local_result_path}")
        except Exception as e:
            logger.warning(f"Failed to download results: {e}")
