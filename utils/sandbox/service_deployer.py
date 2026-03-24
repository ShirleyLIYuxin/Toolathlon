"""
Service deployer for DinD sandboxes.

Manages deployment of infrastructure services (Poste email, Canvas LMS,
WooCommerce, Kind/K8s) inside a Daytona sandbox running Docker-in-Docker.

Services are detected from task_config.json's needed_mcp_servers field
and deployed via docker run inside the sandbox's nested Docker daemon.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Set

if TYPE_CHECKING:
    from utils.sandbox.daytona_executor import DaytonaSandboxExecutor

logger = logging.getLogger(__name__)

# Maps MCP server names → infrastructure service names
SERVICE_MAP = {
    "emails": "poste",
    "canvas": "canvas",
    "woocommerce": "woocommerce",
    "k8s": "kind",
}

# Default ports for services inside the sandbox (matching deployment scripts)
SERVICE_PORTS = {
    "poste": {"web": 10005, "smtp": 2525, "imap": 1143, "submission": 1587},
    "canvas": {"http": 10001, "https": 20001},
    "woocommerce": {"http": 10003},
    "kind": {},
}

# Docker images for each service
SERVICE_IMAGES = {
    "poste": "analogic/poste.io:2.5.5",
    "canvas": "lbjay/canvas-docker",
    "woocommerce": {
        "wordpress": "wordpress:latest",
        "mysql": "mysql:5.7",
    },
    "kind": "kindest/node:v1.27.3",
}


class ServiceDeployer:
    """Deploys infrastructure services inside a DinD sandbox."""

    def __init__(self) -> None:
        self._deployed_services: Set[str] = set()

    def detect_needed_services(self, task_config_path: str) -> set[str]:
        """Detect which infrastructure services a task needs.

        Args:
            task_config_path: Path to task_config.json (local filesystem).

        Returns:
            Set of service names (e.g., {"poste", "canvas"}).
        """
        try:
            with open(task_config_path, "r") as f:
                config = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read task config {task_config_path}: {e}")
            return set()

        needed_servers = config.get("needed_mcp_servers", []) or []
        services = set()
        for server_name in needed_servers:
            if server_name in SERVICE_MAP:
                services.add(SERVICE_MAP[server_name])
        return services

    async def deploy_services(
        self, executor: "DaytonaSandboxExecutor", services: set[str]
    ) -> None:
        """Deploy all needed services inside the sandbox.

        Args:
            executor: The sandbox executor to run commands in.
            services: Set of service names to deploy.
        """
        if not services:
            logger.info("No infrastructure services needed for this task")
            return

        logger.info(f"Deploying services: {services}")

        # Start Docker daemon first (required for all services)
        await self._start_docker_daemon(executor)

        # Deploy each service
        deploy_methods = {
            "poste": self._deploy_poste,
            "canvas": self._deploy_canvas,
            "woocommerce": self._deploy_woocommerce,
            "kind": self._deploy_kind,
        }

        for service in services:
            if service in deploy_methods:
                logger.info(f"Deploying service: {service}")
                await deploy_methods[service](executor)
                self._deployed_services.add(service)
                logger.info(f"Service {service} deployed successfully")
            else:
                logger.warning(f"Unknown service: {service}")

    async def _start_docker_daemon(self, executor: "DaytonaSandboxExecutor") -> None:
        """Start the Docker daemon inside the sandbox (DinD)."""
        # Check if dockerd is already running
        check = await executor.exec("pgrep dockerd", timeout_sec=10)
        if check.return_code == 0:
            logger.info("Docker daemon already running in sandbox")
            return

        # Install dockerd if not present
        check = await executor.exec("which dockerd", timeout_sec=10)
        if check.return_code != 0:
            logger.info("dockerd not found, installing docker.io...")
            install = await executor.exec(
                "apt-get update -qq && apt-get install -y -qq docker.io >/dev/null 2>&1",
                timeout_sec=120,
            )
            if install.return_code != 0:
                logger.error(f"Failed to install docker.io: {install.stderr[-500:]}")
                raise RuntimeError("Failed to install docker.io in sandbox")
            logger.info("docker.io installed successfully")

        logger.info("Starting Docker daemon in sandbox...")

        # Start dockerd in background
        result = await executor.exec(
            "nohup dockerd --storage-driver=overlay2 > /var/log/dockerd.log 2>&1 &",
            timeout_sec=10,
        )

        # Wait for Docker to be ready
        ready = await self._wait_for_service(
            executor,
            name="dockerd",
            check_cmd="docker info",
            timeout=60,
        )
        if not ready:
            # Dump logs for debugging
            logs = await executor.exec("cat /var/log/dockerd.log", timeout_sec=10)
            logger.error(f"Docker daemon logs:\n{logs.stdout}\n{logs.stderr}")
            raise RuntimeError("Docker daemon failed to start in sandbox")

        logger.info("Docker daemon is ready")

    async def _deploy_poste(self, executor: "DaytonaSandboxExecutor") -> None:
        """Deploy Poste.io email server inside the sandbox."""
        ports = SERVICE_PORTS["poste"]
        image = SERVICE_IMAGES["poste"]

        # Create data directory
        await executor.exec("mkdir -p /data/poste", timeout_sec=10)

        # Run Poste container
        cmd = (
            f"docker run -d --name poste "
            f"--cap-add NET_ADMIN --cap-add NET_RAW "
            f"--cap-add NET_BIND_SERVICE --cap-add SYS_PTRACE "
            f"-p {ports['web']}:80 "
            f"-p {ports['smtp']}:25 "
            f"-p {ports['imap']}:143 "
            f"-p {ports['submission']}:587 "
            f'-e "DISABLE_CLAMAV=TRUE" '
            f'-e "DISABLE_RSPAMD=TRUE" '
            f'-e "DISABLE_P0F=TRUE" '
            f'-e "HTTPS_FORCE=0" '
            f'-e "HTTPS=OFF" '
            f"-v /data/poste:/data:Z "
            f"--hostname mcp.com "
            f"{image}"
        )
        result = await executor.exec(cmd, timeout_sec=120)
        if result.return_code != 0:
            raise RuntimeError(f"Failed to start Poste: {result.stderr}")

        # Wait for Poste to be ready
        ready = await self._wait_for_service(
            executor,
            name="poste",
            check_cmd=f"curl -sf http://localhost:{ports['web']}/ > /dev/null",
            timeout=120,
        )
        if not ready:
            raise RuntimeError("Poste.io failed to become ready")

        # Configure dovecot for plaintext auth (matching setup.sh)
        config_cmds = [
            "docker exec poste sed -i 's/ssl = required/ssl = yes/' /etc/dovecot/conf.d/10-ssl.conf",
            "docker exec poste sed -i 's/auth_allow_cleartext = no/auth_allow_cleartext = yes/' /etc/dovecot/conf.d/10-auth.conf",
            "docker exec poste sed -i '/disable_plaintext_auth/d' /etc/dovecot/conf.d/10-auth.conf",
            "docker exec poste sed -i 's/tls_required = true/tls_required = false/' /opt/haraka-smtp/config/auth.ini",
            "docker exec poste sed -i 's/tls_required = true/tls_required = false/' /opt/haraka-submission/config/auth.ini",
            "docker exec poste sed -i 's/^auth\\/poste/#auth\\/poste/' /opt/haraka-submission/config/plugins",
            'docker exec poste sh -c \'echo "127.0.0.1/8" > /opt/haraka-submission/config/relay_acl_allow\'',
            'docker exec poste sh -c \'echo "192.168.0.0/16" >> /opt/haraka-submission/config/relay_acl_allow\'',
            'docker exec poste sh -c \'echo "172.16.0.0/12" >> /opt/haraka-submission/config/relay_acl_allow\'',
            'docker exec poste sh -c \'echo "10.0.0.0/8" >> /opt/haraka-submission/config/relay_acl_allow\'',
        ]
        for cmd in config_cmds:
            await executor.exec(cmd, timeout_sec=30)

        # Reload services
        await executor.exec(
            "docker exec poste doveadm reload 2>/dev/null || true", timeout_sec=30
        )

        # Wait for Poste internal services (dovecot/IMAP) to be ready
        # HTTP readiness doesn't guarantee IMAP is accepting connections
        logger.info("Waiting for Poste IMAP to be ready...")
        imap_ready = await self._wait_for_service(
            executor,
            name="poste-imap",
            check_cmd=f"bash -c 'echo | nc -w3 localhost {ports['imap']} && echo OK'",
            timeout=120,
        )
        if not imap_ready:
            logger.warning("Poste IMAP may not be fully ready, continuing with user creation...")

        # Create user accounts using the deployment script (if uploaded)
        create_users_result = await executor.exec(
            "test -f /workspace/deployment/poste/scripts/create_users.sh && "
            "bash /workspace/deployment/poste/scripts/create_users.sh 503 || "
            "echo 'create_users.sh not found, skipping user creation'",
            timeout_sec=600,
        )
        logger.info(f"User creation exit={create_users_result.return_code}: {create_users_result.stdout[-300:]}")

    async def _deploy_canvas(self, executor: "DaytonaSandboxExecutor") -> None:
        """Deploy Canvas LMS inside the sandbox."""
        ports = SERVICE_PORTS["canvas"]
        image = SERVICE_IMAGES["canvas"]

        cmd = (
            f"docker run -d --name canvas "
            f"-p {ports['http']}:80 "
            f"-p {ports['https']}:443 "
            f"{image}"
        )
        result = await executor.exec(cmd, timeout_sec=120)
        if result.return_code != 0:
            raise RuntimeError(f"Failed to start Canvas: {result.stderr}")

        # Wait for Canvas to be ready
        ready = await self._wait_for_service(
            executor,
            name="canvas",
            check_cmd=f"curl -sf -k https://localhost:{ports['https']}/ > /dev/null",
            timeout=180,
        )
        if not ready:
            logger.warning("Canvas may not be fully ready yet, continuing...")

    async def _deploy_woocommerce(self, executor: "DaytonaSandboxExecutor") -> None:
        """Deploy WooCommerce (WordPress + MySQL) inside the sandbox."""
        ports = SERVICE_PORTS["woocommerce"]

        # Start MySQL
        mysql_cmd = (
            "docker run -d --name woo-mysql "
            '-e "MYSQL_ROOT_PASSWORD=wordpress" '
            '-e "MYSQL_DATABASE=wordpress" '
            '-e "MYSQL_USER=wordpress" '
            '-e "MYSQL_PASSWORD=wordpress" '
            "mysql:5.7"
        )
        result = await executor.exec(mysql_cmd, timeout_sec=120)
        if result.return_code != 0:
            raise RuntimeError(f"Failed to start MySQL: {result.stderr}")

        # Wait for MySQL
        ready = await self._wait_for_service(
            executor,
            name="mysql",
            check_cmd='docker exec woo-mysql mysqladmin ping -h localhost -u root -pwordpress 2>/dev/null',
            timeout=120,
        )
        if not ready:
            raise RuntimeError("MySQL failed to become ready")

        # Start WordPress with WooCommerce
        wp_cmd = (
            f"docker run -d --name woocommerce "
            f"--link woo-mysql:mysql "
            f"-p {ports['http']}:80 "
            f'-e "WORDPRESS_DB_HOST=mysql" '
            f'-e "WORDPRESS_DB_USER=wordpress" '
            f'-e "WORDPRESS_DB_PASSWORD=wordpress" '
            f'-e "WORDPRESS_DB_NAME=wordpress" '
            f"wordpress:latest"
        )
        result = await executor.exec(wp_cmd, timeout_sec=120)
        if result.return_code != 0:
            raise RuntimeError(f"Failed to start WooCommerce: {result.stderr}")

        ready = await self._wait_for_service(
            executor,
            name="woocommerce",
            check_cmd=f"curl -sf http://localhost:{ports['http']}/ > /dev/null",
            timeout=120,
        )
        if not ready:
            logger.warning("WooCommerce may not be fully ready yet, continuing...")

    async def _deploy_kind(self, executor: "DaytonaSandboxExecutor") -> None:
        """Deploy Kind Kubernetes cluster inside the sandbox."""
        # Install kind if not present
        check = await executor.exec("which kind", timeout_sec=10)
        if check.return_code != 0:
            logger.info("Installing Kind...")
            await executor.exec(
                "curl -Lo /usr/local/bin/kind "
                "https://kind.sigs.k8s.io/dl/v0.20.0/kind-linux-amd64 && "
                "chmod +x /usr/local/bin/kind",
                timeout_sec=120,
            )

        # Install kubectl if not present
        check = await executor.exec("which kubectl", timeout_sec=10)
        if check.return_code != 0:
            logger.info("Installing kubectl...")
            await executor.exec(
                "curl -LO 'https://dl.k8s.io/release/v1.27.3/bin/linux/amd64/kubectl' && "
                "chmod +x kubectl && mv kubectl /usr/local/bin/",
                timeout_sec=120,
            )

        # Create Kind cluster
        result = await executor.exec(
            "kind create cluster --name toolathlon --wait 5m",
            timeout_sec=600,
        )
        if result.return_code != 0:
            raise RuntimeError(f"Failed to create Kind cluster: {result.stderr}")

        # Verify cluster
        ready = await self._wait_for_service(
            executor,
            name="kind",
            check_cmd="kubectl cluster-info",
            timeout=60,
        )
        if not ready:
            raise RuntimeError("Kind cluster failed to become ready")

    async def _wait_for_service(
        self,
        executor: "DaytonaSandboxExecutor",
        name: str,
        check_cmd: str,
        timeout: int = 120,
    ) -> bool:
        """Wait for a service to become ready.

        Args:
            executor: Sandbox executor.
            name: Service name (for logging).
            check_cmd: Command that returns 0 when service is ready.
            timeout: Maximum wait time in seconds.

        Returns:
            True if service became ready, False if timed out.
        """
        elapsed = 0
        interval = 3
        while elapsed < timeout:
            result = await executor.exec(check_cmd, timeout_sec=15)
            if result.return_code == 0:
                return True
            await asyncio.sleep(interval)
            elapsed += interval
            if elapsed % 15 == 0:
                logger.info(f"Waiting for {name}... ({elapsed}/{timeout}s)")
        return False

    @property
    def deployed_services(self) -> set[str]:
        return self._deployed_services.copy()
