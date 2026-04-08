"""
Daytona decoupled executor for Toolathlon.

Runs the decoupled agent loop on a Daytona cloud sandbox:
1. Upload project files to sandbox
2. Detect & deploy infrastructure services (DinD)
3. Run container_preprocess.py in sandbox
4. Start container_tool_gateway.py in sandbox, get preview URL
5. Run host_agent_loop.py locally (subprocess on host)
6. Run container_eval.py in sandbox
7. Download results

The key difference from the containerized executor is that the agent loop
runs on the host machine, connecting to tools in the sandbox via SSE over
Daytona's preview URL proxy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from utils.sandbox.base_executor import ExecutionResult, SandboxConfig
from utils.sandbox.daytona_executor import DaytonaSandboxExecutor
from utils.sandbox.service_deployer import ServiceDeployer

logger = logging.getLogger(__name__)

# Legacy defaults — actual values come from SandboxConfig fields:
#   config.daytona_gateway_port (default 10086)
#   config.daytona_gateway_startup_timeout (default 120)
#   config.daytona_preprocess_timeout (default 300)
#   config.daytona_eval_timeout (default 300)


class DaytonaDecoupledExecutor(DaytonaSandboxExecutor):
    """
    Daytona executor for the decoupled agent loop mode.

    In decoupled mode:
    - Preprocessing, gateway, and evaluation run inside the sandbox
    - The agent loop runs on the host, connecting via SSE preview URL
    - Infrastructure services (email, canvas, etc.) deploy inside DinD
    """

    def __init__(self, config: SandboxConfig):
        super().__init__(config)
        self._service_deployer = ServiceDeployer()
        self._gateway_url: Optional[str] = None

    @property
    def executor_type(self) -> str:
        return "daytona_decoupled"

    async def _run_task_internal(self) -> ExecutionResult:
        """
        Override to implement the decoupled six-step flow:
        1. Upload project files
        2. Detect & deploy services (DinD)
        3. Run container_preprocess.py
        4. Start container_tool_gateway.py, get preview URL
        5. Run host_agent_loop.py locally
        6. Run container_eval.py
        7. Download results
        """
        task_parts = self.config.task_dir.split("/")
        tasks_folder = task_parts[0] if len(task_parts) >= 2 else ""
        task_name = task_parts[1] if len(task_parts) >= 2 else self.config.task_dir
        local_output_dir = (Path(self.config.dump_path) / tasks_folder / task_name).resolve()
        local_output_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Step 1: Upload project files
            logger.info(f"[{self.config.task_dir}] Step 1: Uploading project files...")
            await self._upload_project_files()
            # Also upload deployment scripts for service deployer
            await self._upload_deployment_files()

            # Step 2: Detect & deploy services
            logger.info(f"[{self.config.task_dir}] Step 2: Detecting and deploying services...")
            await self._deploy_services()

            # Step 3: Run preprocess
            logger.info(f"[{self.config.task_dir}] Step 3: Running preprocess...")
            await self._run_preprocess(str(local_output_dir))

            # Step 3.5: Download task_bundle.json to host (needed by host agent loop)
            logger.info(f"[{self.config.task_dir}] Step 3.5: Downloading task_bundle.json to host...")
            bundle_path = local_output_dir / "task_bundle.json"
            await self.download_file(
                "/workspace/dumps/task_bundle.json",
                bundle_path,
            )
            if not bundle_path.exists():
                raise RuntimeError(
                    f"task_bundle.json not found at {bundle_path} after download"
                )
            logger.info(f"[{self.config.task_dir}] task_bundle.json downloaded to {bundle_path}")

            # Step 3.6: Download agent workspace to host
            # The host_agent_loop does os.chdir(agent_workspace), so the directory
            # must exist locally. In Docker decoupled mode this is shared via volume
            # mount; in Daytona mode we need to explicitly download it.
            host_workspace = local_output_dir / "workspace"
            host_workspace.mkdir(parents=True, exist_ok=True)
            logger.info(f"[{self.config.task_dir}] Step 3.6: Downloading workspace to host...")
            try:
                await self.download_dir("/workspace/dumps/workspace", host_workspace)
                logger.info(f"[{self.config.task_dir}] Workspace downloaded to {host_workspace}")
            except Exception as e:
                logger.warning(f"[{self.config.task_dir}] Workspace download failed (may be empty): {e}")

            # Step 4: Start gateway and get URL
            logger.info(f"[{self.config.task_dir}] Step 4: Starting gateway...")
            gateway_url = await self._start_gateway()
            self._gateway_url = gateway_url
            logger.info(f"[{self.config.task_dir}] Gateway URL: {gateway_url}")

            # Step 5: Run host agent loop
            logger.info(f"[{self.config.task_dir}] Step 5: Running host agent loop...")
            host_loop_exit = await self._run_host_agent_loop(
                bundle_path=str(bundle_path),
                gateway_url=gateway_url,
                log_path=str(local_output_dir / "host_loop.log"),
            )
            logger.info(f"[{self.config.task_dir}] Host agent loop exit code: {host_loop_exit}")

            # Step 5.5: Upload traj_log.json and workspace from host back to sandbox
            traj_log_path = local_output_dir / "traj_log.json"
            if traj_log_path.exists():
                logger.info(f"[{self.config.task_dir}] Step 5.5: Uploading traj_log.json to sandbox...")
                await self.upload_file(traj_log_path, "/workspace/dumps/traj_log.json")
            else:
                logger.warning(f"[{self.config.task_dir}] traj_log.json not found at {traj_log_path}, eval may fail")

            # NOTE: Do NOT upload host workspace back to sandbox here.
            # The agent modifies files in the sandbox via MCP filesystem server,
            # so the sandbox has the correct state. Uploading the stale host copy
            # would overwrite the agent's work.

            # Step 6: Run evaluation
            logger.info(f"[{self.config.task_dir}] Step 6: Running evaluation...")
            eval_result = await self._run_eval()

            # Step 7: Download results
            logger.info(f"[{self.config.task_dir}] Step 7: Downloading results...")
            await self._download_results(local_output_dir)

            # Read eval result from local
            eval_data = self._read_eval_result_from_local()

            return ExecutionResult(
                success=host_loop_exit == 0,
                return_code=host_loop_exit,
                log_content=f"Decoupled execution completed. Gateway: {gateway_url}",
                eval_passed=eval_data.get("pass") if eval_data else None,
                eval_result=eval_data,
            )

        except Exception as e:
            logger.error(f"[{self.config.task_dir}] Decoupled execution failed: {e}")
            # Try to download whatever results exist
            try:
                await self._download_results(local_output_dir)
            except Exception as dl_err:
                logger.warning(f"[{self.config.task_dir}] Failed to download results after error: {dl_err}")
            raise

    async def _upload_deployment_files(self) -> None:
        """Upload deployment scripts needed for service deployment."""
        if not self._project_root:
            self._project_root = Path.cwd()

        project_root = self._project_root

        # Upload deployment directories needed for DinD service setup
        deployment_dirs = [
            "deployment/poste",
            "deployment/canvas",
            "deployment/woocommerce",
            "deployment/k8s",
        ]
        for dep_dir in deployment_dirs:
            source = project_root / dep_dir
            if source.exists():
                target = f"/workspace/{dep_dir}"
                await self.exec(f"mkdir -p {target}")
                await self.upload_dir(source, target)
                logger.debug(f"Uploaded deployment: {dep_dir}")

        # Upload users data for email service
        users_data = project_root / "configs" / "users_data.json"
        if users_data.exists():
            await self.upload_file(users_data, "/workspace/configs/users_data.json")

    async def _deploy_services(self) -> None:
        """Detect and deploy needed infrastructure services."""
        if not self._project_root:
            self._project_root = Path.cwd()

        # Detect needed services from task config
        task_parts = self.config.task_dir.split("/")
        if len(task_parts) >= 2:
            task_config_path = (
                self._project_root / "tasks" / task_parts[0] / task_parts[1] / "task_config.json"
            )
        else:
            task_config_path = self._project_root / "tasks" / self.config.task_dir / "task_config.json"

        services = self._service_deployer.detect_needed_services(str(task_config_path))

        if services:
            logger.info(f"Detected services needed: {services}")
            await self._service_deployer.deploy_services(self, services)
        else:
            logger.info("No infrastructure services needed")

    async def _run_preprocess(self, host_output_folder: str) -> None:
        """Run container_preprocess.py in the sandbox."""
        import shlex
        quoted_host_output = shlex.quote(host_output_folder)
        logger.info(f"Preprocess host_output_folder: {host_output_folder}")
        cmd = (
            f"uv run python -m scripts.decoupled.container_preprocess "
            f"--eval_config {shlex.quote(self.config.eval_config)} "
            f"--task_dir {shlex.quote(self.config.task_dir)} "
            f"--max_steps_under_single_turn_mode {self.config.max_steps} "
            f"--model_short_name {shlex.quote(self.config.model_short_name)} "
            f"--provider {shlex.quote(self.config.provider)} "
            f"--bundle_file /workspace/dumps/task_bundle.json "
            f"--host_output_folder {quoted_host_output} "
            f"--debug"
        )
        result = await self.exec(cmd, cwd="/workspace", timeout_sec=self.config.daytona_preprocess_timeout)
        if result.return_code != 0:
            logger.error(f"Preprocess stdout: {result.stdout[-2000:]}")
            logger.error(f"Preprocess stderr: {result.stderr[-2000:]}")
            raise RuntimeError(
                f"Preprocess failed (exit {result.return_code}): {result.stderr[-500:]}"
            )
        logger.info("Preprocess completed successfully")

    async def _start_gateway(self) -> str:
        """Start the MCP gateway in the sandbox and return the preview URL.

        Returns:
            The gateway URL accessible from the host (via Daytona preview proxy).
        """
        if not self._sandbox:
            raise RuntimeError("Sandbox not started")

        # Start gateway in background
        gateway_cmd = (
            f"nohup uv run python -m scripts.decoupled.container_tool_gateway "
            f"--bundle_file /workspace/dumps/task_bundle.json "
            f"--host 0.0.0.0 --port {self.config.daytona_gateway_port} --debug "
            f"> /workspace/logs/gateway.log 2>&1 & echo $!"
        )
        result = await self.exec(gateway_cmd, cwd="/workspace", timeout_sec=30)
        gateway_pid = result.stdout.strip()
        logger.info(f"Gateway started with PID: {gateway_pid}")

        # Get signed preview URL (includes auth token in subdomain, no redirect)
        gateway_port = self.config.daytona_gateway_port
        preview_result = await self._sandbox.create_signed_preview_url(
            gateway_port, expires_in_seconds=7200
        )
        if not preview_result:
            raise RuntimeError(
                f"Failed to get preview URL for port {gateway_port}"
            )

        # Extract URL string from result object
        if hasattr(preview_result, 'url'):
            preview_url = preview_result.url
        elif isinstance(preview_result, dict) and 'url' in preview_result:
            preview_url = preview_result['url']
        elif isinstance(preview_result, str):
            preview_url = preview_result
        else:
            preview_url = str(preview_result)

        # Ensure URL has proper scheme
        if not preview_url.startswith("http"):
            preview_url = f"https://{preview_url}"

        # Wait for gateway to be ready via the preview URL
        ready = await self._wait_for_gateway(preview_url)
        if not ready:
            # Dump gateway logs for debugging
            logs = await self.exec("cat /workspace/logs/gateway.log", timeout_sec=10)
            logger.error(f"Gateway logs:\n{logs.stdout[-2000:]}")
            raise RuntimeError("Gateway did not become ready")

        return preview_url

    async def _wait_for_gateway(self, preview_url: str) -> bool:
        """Wait for the gateway to become ready via the preview URL.

        Tries both the /health endpoint and basic connectivity.
        """
        health_url = f"{preview_url}/health"
        elapsed = 0
        interval = 3

        timeout = self.config.daytona_gateway_startup_timeout
        while elapsed < timeout:
            try:
                # Use subprocess curl since we're on the host
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-sf", "--max-time", "5", health_url,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode == 0:
                    logger.info(f"Gateway ready: {stdout.decode()[:200]}")
                    return True
            except Exception as e:
                logger.debug(f"Gateway health check failed: {e}")

            await asyncio.sleep(interval)
            elapsed += interval
            if elapsed % 15 == 0:
                logger.info(f"Waiting for gateway... ({elapsed}/{timeout}s)")

        return False

    async def _run_host_agent_loop(
        self,
        bundle_path: str,
        gateway_url: str,
        log_path: str,
    ) -> int:
        """Run the host-side agent loop as a local subprocess.

        Args:
            bundle_path: Path to the task_bundle.json on the host.
            gateway_url: SSE gateway URL (Daytona preview URL).
            log_path: Path to write host loop logs.

        Returns:
            Exit code of the host agent loop.
        """
        gateway_server_name = "gw"
        sse_url = f"{gateway_url}/sse"

        # Build command based on agent framework
        if self.config.agent_framework == "claude_agent_sdk":
            cmd = [
                "uv", "run", "python", "-m",
                "scripts.decoupled.host_agent_loop_claude_sdk",
                "--bundle_file", bundle_path,
                "--gateway_url", sse_url,
                "--gateway_server_name", gateway_server_name,
                "--model", self.config.model_short_name,
                "--tool_call_mode", "parallel",
                "--debug",
            ]
        else:
            # toolathlon_default (OpenAI Agents SDK)
            cmd = [
                "uv", "run", "python", "-m",
                "scripts.decoupled.host_agent_loop",
                "--bundle_file", bundle_path,
                "--gateway_url", sse_url,
                "--gateway_server_name", gateway_server_name,
                "--debug",
            ]

        logger.info(f"Host agent loop command: {' '.join(cmd)}")

        # Ensure log directory exists
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        # Run as subprocess from the project root
        project_root = str(self._project_root or Path.cwd())

        # Build environment - inherit current env plus any needed vars
        env = os.environ.copy()
        # Remove CLAUDECODE to avoid "nested session" detection by claude_agent_sdk
        env.pop("CLAUDECODE", None)

        with open(log_path, "w") as log_file:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=project_root,
                env=env,
            )

            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.config.timeout_sec,
                )
            except asyncio.TimeoutError:
                logger.warning("Host agent loop timed out, terminating...")
                process.terminate()
                await asyncio.sleep(5)
                if process.returncode is None:
                    process.kill()
                return -1

        return process.returncode or 0

    async def _run_eval(self) -> dict:
        """Run container_eval.py in the sandbox.

        Returns:
            Evaluation result dict.
        """
        cmd = (
            "uv run python -m scripts.decoupled.container_eval "
            "--bundle_file /workspace/dumps/task_bundle.json"
        )
        result = await self.exec(cmd, cwd="/workspace", timeout_sec=self.config.daytona_eval_timeout)

        if result.return_code != 0:
            logger.warning(
                f"Eval finished with exit code {result.return_code}: "
                f"{result.stderr[-500:]}"
            )

        # Try to parse eval result from stdout
        eval_data = {}
        for line in result.stdout.splitlines():
            if line.startswith("Pass:"):
                val = line.split(":", 1)[1].strip()
                eval_data["pass"] = val.lower() == "true"
            elif line.startswith("Details:"):
                eval_data["details"] = line.split(":", 1)[1].strip()
            elif line.startswith("Failure:"):
                eval_data["failure"] = line.split(":", 1)[1].strip()

        return eval_data

    async def _download_results(self, local_output_dir: Path) -> None:
        """Download all results from the sandbox to local output directory."""
        local_output_dir.mkdir(parents=True, exist_ok=True)

        # Download dumps directory (contains task_bundle.json, eval_res.json, traj_log.json)
        try:
            await self.download_dir("/workspace/dumps", local_output_dir)
            logger.info(f"Downloaded dumps to {local_output_dir}")
        except Exception as e:
            logger.warning(f"Failed to download dumps: {e}")

        # Download logs
        log_files = [
            ("preprocess.log", "/workspace/logs/preprocess.log"),
            ("gateway.log", "/workspace/logs/gateway.log"),
            ("eval.log", "/workspace/logs/eval.log"),
        ]
        for local_name, remote_path in log_files:
            try:
                await self.download_file(
                    remote_path, local_output_dir / local_name
                )
            except Exception:
                pass  # Logs are best-effort
