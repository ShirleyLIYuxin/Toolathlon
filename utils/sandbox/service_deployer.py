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

# Services that cannot run on Daytona due to platform limitations
UNSUPPORTED_ON_DAYTONA = {
    "kind": "Kind requires device node creation (/dev/mapper/control) which Daytona blocks. "
            "Use Docker backend for K8s tasks. See docs/kind-feasibility-spike.md.",
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
        "wordpress": "wordpress:6.8.2-php8.2-apache",
        "mysql": "mysql:8.0",
    },
    "kind": "kindest/node:v1.27.3",
}

# WooCommerce configuration constants (matching deployment/woocommerce/scripts/setup.sh)
WOO_ADMIN_USER = "mcpwoocommerce"
WOO_ADMIN_PASS = "mcpwoocommerce"
WOO_ADMIN_EMAIL = "woocommerce@mcp.com"
WOO_DB_USER = "wordpress"
WOO_DB_PASS = "wppass123"
WOO_DB_ROOT_PASS = "rootpass123"
WOO_DB_NAME = "wordpress"


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
            if service in UNSUPPORTED_ON_DAYTONA:
                logger.warning(
                    f"Skipping unsupported service '{service}' on Daytona: "
                    f"{UNSUPPORTED_ON_DAYTONA[service]}"
                )
                continue
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

        # Give dockerd a moment to initialize before polling
        await asyncio.sleep(2)

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

        # Create user accounts directly via docker exec + jq, bypassing create_users.sh
        # which has uv run dependency issues in the sandbox.
        logger.info("Creating email user accounts via direct docker exec...")

        # Create domain first
        await executor.exec(
            'docker exec --user=8 poste php /opt/admin/bin/console domain:create mcp.com 2>&1 || true',
            timeout_sec=30,
        )

        # Create admin
        await executor.exec(
            'docker exec --user=8 poste php /opt/admin/bin/console email:create '
            '"mcpposte_admin@mcp.com" "mcpposte" "System Administrator" 2>&1 || true',
            timeout_sec=30,
        )
        await executor.exec(
            'docker exec --user=8 poste php /opt/admin/bin/console email:admin '
            '"mcpposte_admin@mcp.com" 2>&1 || true',
            timeout_sec=30,
        )

        # Create users from users_data.json using jq + while read loop
        # jq expression must use single quotes to avoid shell expansion of \(...)
        create_cmd = (
            "cd /workspace && "
            r"""jq -r '.users[] | "\(.email)|\(.password)|\(.full_name)"' configs/users_data.json"""
            " | head -503 | "
            r"""while IFS='|' read -r email password fullname; do """
            r"""docker exec --user=8 poste php /opt/admin/bin/console email:create "$email" "$password" "$fullname" 2>/dev/null; """
            "done && "
            "echo USERS_CREATED=$(docker exec --user=8 poste php /opt/admin/bin/console email:list 2>/dev/null | wc -l)"
        )
        create_result = await executor.exec(create_cmd, timeout_sec=600)
        logger.info(f"User creation exit={create_result.return_code}: {create_result.stdout[-500:]}")

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
        """Deploy WooCommerce (WordPress + MySQL) inside the sandbox.

        Full setup matching deployment/woocommerce/scripts/setup.sh:
        1. Create Docker network
        2. Start MySQL 8.0
        3. Start WordPress
        4. Install WP-CLI
        5. Install WordPress core + WooCommerce plugin
        6. Configure permalinks, HTTP auth, sales hooks
        7. Generate REST API keys
        8. Convert to multisite + create user subsites
        9. Fix upload permissions
        """
        ports = SERVICE_PORTS["woocommerce"]
        images = SERVICE_IMAGES["woocommerce"]
        wp_port = ports["http"]
        wp_url = f"http://localhost:{wp_port}"

        # Step 1: Create Docker network (replaces deprecated --link)
        await executor.exec("docker network create woo-net 2>/dev/null || true", timeout_sec=10)

        # Step 2: Start MySQL
        logger.info("Starting MySQL for WooCommerce...")
        mysql_cmd = (
            "docker run -d --network woo-net --name woo-db "
            f'-e "MYSQL_ROOT_PASSWORD={WOO_DB_ROOT_PASS}" '
            f'-e "MYSQL_DATABASE={WOO_DB_NAME}" '
            f'-e "MYSQL_USER={WOO_DB_USER}" '
            f'-e "MYSQL_PASSWORD={WOO_DB_PASS}" '
            f"{images['mysql']}"
        )
        result = await executor.exec(mysql_cmd, timeout_sec=120)
        if result.return_code != 0:
            raise RuntimeError(f"Failed to start MySQL: {result.stderr}")

        # Wait for MySQL with actual SQL check
        ready = await self._wait_for_service(
            executor,
            name="mysql",
            check_cmd=f"docker exec woo-db mysql -u {WOO_DB_USER} -p{WOO_DB_PASS} -e 'SELECT 1' 2>/dev/null",
            timeout=120,
        )
        if not ready:
            raise RuntimeError("MySQL failed to become ready")

        # Step 3: Start WordPress
        logger.info("Starting WordPress...")
        wp_cmd = (
            f"docker run -d --network woo-net --name woo-wp "
            f"-p {wp_port}:80 "
            f'-e "WORDPRESS_DB_HOST=woo-db" '
            f'-e "WORDPRESS_DB_USER={WOO_DB_USER}" '
            f'-e "WORDPRESS_DB_PASSWORD={WOO_DB_PASS}" '
            f'-e "WORDPRESS_DB_NAME={WOO_DB_NAME}" '
            f"{images['wordpress']}"
        )
        result = await executor.exec(wp_cmd, timeout_sec=120)
        if result.return_code != 0:
            raise RuntimeError(f"Failed to start WordPress: {result.stderr}")

        ready = await self._wait_for_service(
            executor,
            name="wordpress",
            check_cmd=f"curl -sf -o /dev/null -w '%{{http_code}}' http://localhost:{wp_port}/ | grep -q '302\\|200'",
            timeout=120,
        )
        if not ready:
            raise RuntimeError("WordPress failed to become ready")

        # Step 4: Install WP-CLI
        logger.info("Installing WP-CLI...")
        wpcli_cmd = (
            "docker exec woo-wp bash -c '"
            "curl -sO https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar && "
            "chmod +x wp-cli.phar && mv wp-cli.phar /usr/local/bin/wp'"
        )
        result = await executor.exec(wpcli_cmd, timeout_sec=120)
        if result.return_code != 0:
            raise RuntimeError(f"Failed to install WP-CLI: {result.stderr}")

        # Step 5: Install WordPress core
        logger.info("Configuring WordPress core...")
        wp_install = (
            f"docker exec woo-wp wp core install "
            f'--url="{wp_url}" '
            f'--title="My WooCommerce Store" '
            f"--admin_user={WOO_ADMIN_USER} "
            f"--admin_password={WOO_ADMIN_PASS} "
            f"--admin_email={WOO_ADMIN_EMAIL} "
            f"--skip-email --allow-root --path=/var/www/html"
        )
        result = await executor.exec(wp_install, timeout_sec=120)
        if result.return_code != 0:
            raise RuntimeError(f"WordPress core install failed: {result.stderr}")

        # Step 6: Install and activate WooCommerce plugin
        logger.info("Installing WooCommerce plugin...")
        result = await executor.exec(
            "docker exec woo-wp wp plugin install woocommerce --activate --allow-root --path=/var/www/html",
            timeout_sec=180,
        )
        if result.return_code != 0:
            raise RuntimeError(f"WooCommerce plugin install failed: {result.stderr}")

        # Step 7: Configure permalinks
        logger.info("Configuring permalinks...")
        await executor.exec(
            "docker exec woo-wp wp rewrite structure '/%postname%/' --allow-root --path=/var/www/html",
            timeout_sec=30,
        )
        await executor.exec(
            "docker exec woo-wp wp rewrite flush --allow-root --path=/var/www/html",
            timeout_sec=30,
        )
        await executor.exec(
            "docker exec woo-wp bash -c 'chmod 644 /var/www/html/.htaccess 2>/dev/null || "
            "touch /var/www/html/.htaccess && chmod 644 /var/www/html/.htaccess'",
            timeout_sec=10,
        )

        # Step 8: Configure HTTP auth support
        await executor.exec(
            'docker exec woo-wp bash -c \'echo "SetEnvIf Authorization (.+) HTTPS=on" >> /var/www/html/.htaccess\'',
            timeout_sec=10,
        )

        # Step 9: Add WooCommerce sales display hooks
        logger.info("Adding WooCommerce sales display hooks...")
        hooks_php = (
            "docker exec woo-wp bash -c 'cat >> /var/www/html/wp-content/themes/twentytwentyfive/functions.php << \"EOFPHP\"\n"
            "\n"
            "// Display total sales in shop page\n"
            "add_action( '\"'\"'woocommerce_after_shop_loop_item_title'\"'\"', '\"'\"'wc_product_sold_count'\"'\"', 5 );\n"
            "// Display total sales in product detail page\n"
            "add_action( '\"'\"'woocommerce_single_product_summary'\"'\"', '\"'\"'wc_product_sold_count'\"'\"', 11 );\n"
            "function wc_product_sold_count() {\n"
            "    global $product;\n"
            "    $units_sold = get_post_meta( $product->id, '\"'\"'total_sales'\"'\"', true );\n"
            "    echo '\"'\"'<p>'\"'\"' . sprintf( __( '\"'\"'Total Sales: %s'\"'\"', '\"'\"'woocommerce'\"'\"' ), $units_sold ) . '\"'\"'</p>'\"'\"';\n"
            "}\n"
            "EOFPHP'"
        )
        await executor.exec(hooks_php, timeout_sec=30)

        # Step 10: Generate admin REST API keys
        logger.info("Generating WooCommerce REST API keys...")
        api_key_php = (
            "docker exec woo-wp wp eval '"
            '$user_id = 1; '
            '$consumer_key = "ck_woocommerce_token_admin"; '
            '$consumer_secret = "cs_woocommerce_token_admin"; '
            'global $wpdb; '
            "$wpdb->insert("
            '    $wpdb->prefix . "woocommerce_api_keys",'
            "    array("
            '        \"user_id\" => $user_id,'
            '        \"description\" => \"Auto Generated API Key\",'
            '        \"permissions\" => \"read_write\",'
            '        \"consumer_key\" => wc_api_hash($consumer_key),'
            '        \"consumer_secret\" => $consumer_secret,'
            '        \"truncated_key\" => substr($consumer_key, -7)'
            "    )"
            "); "
            "echo json_encode(array("
            '    \"consumer_key\" => $consumer_key,'
            '    \"consumer_secret\" => $consumer_secret'
            "));"
            "' --allow-root --path=/var/www/html 2>/dev/null"
        )
        result = await executor.exec(api_key_php, timeout_sec=30)
        logger.info(f"API key generation: {result.stdout[:200]}")

        # Step 11: Convert to multisite
        logger.info("Converting to WordPress multisite...")
        result = await executor.exec(
            'docker exec woo-wp wp core multisite-convert --title="My Multisite Network" '
            "--allow-root --path=/var/www/html",
            timeout_sec=60,
        )
        if result.return_code != 0:
            logger.warning(f"Multisite conversion may have issues: {result.stderr[:300]}")

        # Step 12: Update .htaccess for multisite
        htaccess_cmd = (
            "docker exec woo-wp bash -c 'cat > /var/www/html/.htaccess << \"EOF\"\n"
            "# BEGIN WordPress Multisite\n"
            "# Using subfolder network type\n"
            "RewriteEngine On\n"
            "RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]\n"
            "RewriteBase /\n"
            "RewriteRule ^index\\.php$ - [L]\n"
            "\n"
            "# add a trailing slash to /wp-admin\n"
            "RewriteRule ^([_0-9a-zA-Z-]+/)?wp-admin$ $1wp-admin/ [R=301,L]\n"
            "\n"
            "RewriteCond %{REQUEST_FILENAME} -f [OR]\n"
            "RewriteCond %{REQUEST_FILENAME} -d\n"
            "RewriteRule ^ - [L]\n"
            "RewriteRule ^([_0-9a-zA-Z-]+/)?(wp-(content|admin|includes).*) $2 [L]\n"
            "RewriteRule ^([_0-9a-zA-Z-]+/)?(.*\\.php)$ $2 [L]\n"
            "RewriteRule . index.php [L]\n"
            "\n"
            "# END WordPress Multisite\n"
            "SetEnvIf Authorization (.+) HTTPS=on\n"
            "EOF'"
        )
        await executor.exec(htaccess_cmd, timeout_sec=10)

        # Step 13: Network activate WooCommerce
        await executor.exec(
            "docker exec woo-wp wp plugin activate woocommerce --network --allow-root --path=/var/www/html",
            timeout_sec=30,
        )

        # Step 14: Create multisite subsites with user data
        logger.info("Creating multisite subsites from user data...")
        await self._create_woocommerce_subsites(executor, wp_url, wp_port)

        # Step 15: Fix upload permissions
        logger.info("Fixing upload permissions...")
        await executor.exec(
            "docker exec woo-wp bash -c '"
            "mkdir -p /var/www/html/wp-content/uploads/$(date +%Y)/$(date +%m) && "
            "chown -R www-data:www-data /var/www/html/wp-content/uploads/ && "
            "find /var/www/html/wp-content/uploads/ -type d -exec chmod 755 {} \\; && "
            "find /var/www/html/wp-content/uploads/ -type f -exec chmod 644 {} \\;'",
            timeout_sec=30,
        )

        logger.info("WooCommerce deployment completed")

    async def _create_woocommerce_subsites(
        self,
        executor: "DaytonaSandboxExecutor",
        wp_url: str,
        wp_port: int,
    ) -> None:
        """Create WooCommerce multisite subsites from configs/users_data.json.

        Reads the uploaded users_data.json and creates a subsite + API key
        for each user, matching deployment/woocommerce/scripts/setup.sh.
        """
        # Read user data from the sandbox (uploaded by _upload_project_files)
        result = await executor.exec(
            "cat /workspace/configs/users_data.json 2>/dev/null", timeout_sec=10
        )
        if result.return_code != 0 or not result.stdout.strip():
            logger.warning("users_data.json not found in sandbox, skipping subsite creation")
            return

        try:
            users_data = json.loads(result.stdout)
            users = users_data.get("users", [])
        except json.JSONDecodeError:
            logger.warning("Failed to parse users_data.json, skipping subsite creation")
            return

        # Create first 20 subsites (matching default setup.sh behavior)
        num_sites = min(len(users), 20)
        logger.info(f"Creating {num_sites} WooCommerce subsites...")

        created = 0
        for user in users[:num_sites]:
            user_id = user.get("id", "")
            first_name = user.get("first_name", "")
            last_name = user.get("last_name", "")
            email = user.get("email", "")
            ck = user.get("woocommerce_consumer_key", "")
            cs = user.get("woocommerce_consumer_secret", "")

            site_slug = f"store{user_id}"
            site_title = f"{first_name} {last_name}'s Store"
            site_url = f"{wp_url}/{site_slug}/"

            # Create subsite
            create_result = await executor.exec(
                f'docker exec woo-wp wp site create --slug="{site_slug}" '
                f'--title="{site_title}" --email="{email}" '
                f"--allow-root --path=/var/www/html 2>&1",
                timeout_sec=30,
            )
            if "Success" not in create_result.stdout and create_result.return_code != 0:
                logger.debug(f"Subsite {site_slug} creation issue: {create_result.stdout[:200]}")
                continue

            # Insert API key for this subsite
            if ck and cs:
                api_insert_php = (
                    f"docker exec woo-wp wp eval '"
                    f'$user_id = 1; '
                    f'$consumer_key = "{ck}"; '
                    f'$consumer_secret = "{cs}"; '
                    f'global $wpdb; '
                    f"$wpdb->insert("
                    f'    $wpdb->prefix . "woocommerce_api_keys",'
                    f"    array("
                    f'        \"user_id\" => $user_id,'
                    f'        \"description\" => \"{site_slug} API Key\",'
                    f'        \"permissions\" => \"read_write\",'
                    f'        \"consumer_key\" => wc_api_hash($consumer_key),'
                    f'        \"consumer_secret\" => $consumer_secret,'
                    f'        \"truncated_key\" => substr($consumer_key, -7)'
                    f"    )"
                    f"); "
                    f'echo $wpdb->last_error ? "ERROR" : "SUCCESS";'
                    f"' --url=\"{site_url}\" --allow-root --path=/var/www/html 2>/dev/null"
                )
                await executor.exec(api_insert_php, timeout_sec=30)

            created += 1

        logger.info(f"Created {created}/{num_sites} WooCommerce subsites")

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
