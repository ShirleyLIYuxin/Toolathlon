#!/usr/bin/env python3
"""
Kind feasibility spike: test if Kind can create a cluster in a Daytona sandbox.

Tests:
1. Basic kind create cluster (will fail with --privileged)
2. Kind with rootless mode
3. Kind with podman provider
4. Direct kubeadm approach (bypass kind entirely)
"""
import asyncio, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "configs"))
from global_configs import global_configs
os.environ["DAYTONA_API_KEY"] = global_configs.get("daytona_api_key", "")

from daytona import AsyncDaytona, CreateSandboxFromSnapshotParams, SessionExecuteRequest

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def run_cmd(sb, cmd, timeout=60, sid="m"):
    r = await sb.process.execute_session_command(
        sid, SessionExecuteRequest(command=cmd, run_async=True), timeout=timeout
    )
    deadline = time.time() + timeout
    cr = await sb.process.get_session_command(sid, r.cmd_id)
    while cr.exit_code is None:
        if time.time() > deadline:
            return -1, "(timeout)"
        await asyncio.sleep(2)
        cr = await sb.process.get_session_command(sid, cr.id)
    logs = await sb.process.get_session_command_logs(sid, r.cmd_id)
    return int(cr.exit_code), (logs.stdout or "").strip()


async def main():
    async with AsyncDaytona() as d:
        params = CreateSandboxFromSnapshotParams(
            snapshot="toolathlon-dind-v1",
            auto_stop_interval=0, auto_delete_interval=0,
            labels={"project": "toolathlon-kind-spike"},
        )
        sb = await d.create(params=params, timeout=300)
        logger.info(f"Sandbox: {sb.id}")

        await sb.process.create_session("d")
        await sb.process.create_session("m")

        async def run(cmd, timeout=60):
            return await run_cmd(sb, cmd, timeout=timeout, sid="m")

        try:
            # Start dockerd
            await run_cmd(sb, "dockerd --storage-driver=overlay2 > /var/log/dockerd.log 2>&1",
                          timeout=5, sid="d")
            for i in range(15):
                rc, _ = await run("docker info > /dev/null 2>&1")
                if rc == 0:
                    break
                await asyncio.sleep(2)
            logger.info(f"dockerd ready: {rc == 0}")

            # Check capabilities
            logger.info("=== Checking sandbox capabilities ===")
            rc, out = await run("cat /proc/self/status | grep -i cap")
            logger.info(f"Capabilities:\n{out}")

            rc, out = await run("ls -la /dev/mapper/ 2>&1 | head -5")
            logger.info(f"/dev/mapper: {out}")

            rc, out = await run("mount | grep cgroup | head -5")
            logger.info(f"cgroups: {out}")

            # Install kind
            logger.info("=== Installing kind ===")
            rc, out = await run(
                "curl -Lo /usr/local/bin/kind https://kind.sigs.k8s.io/dl/v0.20.0/kind-linux-amd64 && "
                "chmod +x /usr/local/bin/kind && kind version",
                timeout=60,
            )
            logger.info(f"kind install: exit={rc} {out[-100:]}")

            # Test 1: Standard kind create cluster
            logger.info("=== Test 1: Standard kind create cluster ===")
            rc, out = await run(
                "kind create cluster --name test1 --wait 2m 2>&1",
                timeout=180,
            )
            logger.info(f"Test 1 result: exit={rc}")
            logger.info(f"Output: {out[-500:]}")

            if rc != 0:
                # Test 2: Try with explicit docker provider
                logger.info("=== Test 2: kind with explicit docker provider ===")
                rc, out = await run(
                    "KIND_EXPERIMENTAL_PROVIDER=docker kind create cluster --name test2 --wait 2m 2>&1",
                    timeout=180,
                )
                logger.info(f"Test 2 result: exit={rc}")
                logger.info(f"Output: {out[-500:]}")

            if rc != 0:
                # Test 3: Check what docker run --privileged does
                logger.info("=== Test 3: Direct docker run --privileged test ===")
                rc, out = await run(
                    "docker run --rm --privileged alpine:latest echo 'privileged works' 2>&1",
                    timeout=60,
                )
                logger.info(f"Privileged test: exit={rc} {out}")

                # Test 4: docker run without --privileged
                rc, out = await run(
                    "docker run --rm alpine:latest echo 'non-privileged works' 2>&1",
                    timeout=60,
                )
                logger.info(f"Non-privileged test: exit={rc} {out}")

            # Final check: if any test succeeded
            rc, out = await run("kubectl cluster-info 2>&1 || echo 'no cluster'")
            logger.info(f"kubectl cluster-info: {out[:200]}")

        finally:
            await sb.delete()
            logger.info("Sandbox deleted")


asyncio.run(main())
