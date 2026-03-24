#!/usr/bin/env python3
"""
Prepare a Daytona snapshot with pre-cached Docker images for DinD.

This script:
1. Creates a sandbox from the Toolathlon base image
2. Installs Docker (if not present)
3. Starts dockerd
4. Pulls all service Docker images (poste, canvas, wordpress, mysql, kindest/node)
5. Uploads deployment scripts and configs
6. Creates a snapshot for fast task startup

Usage:
    uv run python scripts/prepare_daytona_snapshot.py \
        --snapshot_name toolathlon-dind-v1 \
        [--base_image lockon0927/toolathlon-task-image:1016beta] \
        [--target us]
"""

import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Docker images to pre-pull into the snapshot
IMAGES_TO_PULL = [
    "analogic/poste.io:2.5.5",
    "lbjay/canvas-docker",
    "wordpress:latest",
    "mysql:5.7",
    "kindest/node:v1.27.3",
]


async def run_in_sandbox(sandbox, cmd: str, timeout: int = 300) -> tuple[str, str, int]:
    """Execute a command in the sandbox and return (stdout, stderr, exit_code)."""
    from uuid import uuid4
    from daytona import SessionExecuteRequest

    session_id = str(uuid4())
    await sandbox.process.create_session(session_id)

    response = await sandbox.process.execute_session_command(
        session_id,
        SessionExecuteRequest(command=cmd, run_async=True),
        timeout=timeout,
    )

    if response.cmd_id is None:
        raise RuntimeError("Cannot find command ID")

    # Poll for completion
    result = await sandbox.process.get_session_command(session_id, response.cmd_id)
    while result.exit_code is None:
        await asyncio.sleep(2)
        result = await sandbox.process.get_session_command(session_id, result.id)

    logs = await sandbox.process.get_session_command_logs(session_id, response.cmd_id)
    return (logs.stdout or "", logs.stderr or "", int(result.exit_code))


async def main():
    parser = argparse.ArgumentParser(description="Prepare Daytona DinD snapshot")
    parser.add_argument(
        "--snapshot_name",
        required=True,
        help="Name for the snapshot (e.g., toolathlon-dind-v1)",
    )
    parser.add_argument(
        "--base_image",
        default="lockon0927/toolathlon-task-image:1016beta",
        help="Base Docker image",
    )
    parser.add_argument("--target", default="us", help="Daytona target region")
    parser.add_argument("--cpu", type=int, default=4, help="CPU cores")
    parser.add_argument("--memory", type=int, default=8, help="Memory in GB")
    parser.add_argument("--disk", type=int, default=30, help="Disk in GB")
    args = parser.parse_args()

    try:
        from daytona import (
            AsyncDaytona,
            CreateSandboxFromImageParams,
            Image,
            Resources,
        )
    except ImportError:
        print("Error: daytona package not installed. Run: pip install daytona")
        sys.exit(1)

    logger.info(f"Creating sandbox from image: {args.base_image}")

    async with AsyncDaytona() as daytona:
        # Step 1: Create sandbox
        params = CreateSandboxFromImageParams(
            image=Image.base(args.base_image),
            resources=Resources(cpu=args.cpu, memory=args.memory, disk=args.disk),
            auto_stop_interval=0,
            auto_delete_interval=0,
            labels={"project": "toolathlon-snapshot", "type": "dind"},
        )
        sandbox = await daytona.create(params=params, timeout=600)
        logger.info(f"Sandbox created: {sandbox.id}")

        try:
            # Step 2: Install Docker if needed
            stdout, _, rc = await run_in_sandbox(sandbox, "which dockerd")
            if rc != 0:
                logger.info("Installing Docker...")
                install_cmd = (
                    "apt-get update && "
                    "apt-get install -y docker.io && "
                    "systemctl enable docker || true"
                )
                stdout, stderr, rc = await run_in_sandbox(
                    sandbox, install_cmd, timeout=300
                )
                if rc != 0:
                    logger.error(f"Docker install failed: {stderr}")
                    raise RuntimeError("Failed to install Docker")
                logger.info("Docker installed")
            else:
                logger.info("Docker already installed")

            # Step 3: Start dockerd
            logger.info("Starting Docker daemon...")
            await run_in_sandbox(
                sandbox,
                "nohup dockerd --storage-driver=overlay2 > /var/log/dockerd.log 2>&1 &",
                timeout=10,
            )

            # Wait for dockerd
            for i in range(30):
                _, _, rc = await run_in_sandbox(sandbox, "docker info", timeout=10)
                if rc == 0:
                    break
                await asyncio.sleep(2)
            else:
                raise RuntimeError("Docker daemon failed to start")
            logger.info("Docker daemon is ready")

            # Step 4: Pull all service images
            for image in IMAGES_TO_PULL:
                logger.info(f"Pulling image: {image}")
                stdout, stderr, rc = await run_in_sandbox(
                    sandbox, f"docker pull {image}", timeout=600
                )
                if rc != 0:
                    logger.warning(f"Failed to pull {image}: {stderr[:200]}")
                else:
                    logger.info(f"Pulled: {image}")

            # Step 5: Create workspace directories
            await run_in_sandbox(
                sandbox,
                "mkdir -p /workspace/dumps /workspace/logs /workspace/tasks "
                "/workspace/deployment /workspace/configs",
            )

            # Step 6: Stop dockerd cleanly (images persist on disk)
            logger.info("Stopping Docker daemon...")
            await run_in_sandbox(sandbox, "kill $(pgrep dockerd) || true", timeout=10)
            await asyncio.sleep(3)

            # Step 7: Create snapshot
            logger.info(f"Creating snapshot: {args.snapshot_name}")
            snapshot = await daytona.snapshot.create(
                sandbox_id=sandbox.id,
                snapshot_name=args.snapshot_name,
            )
            logger.info(f"Snapshot created: {args.snapshot_name}")

            # Wait for snapshot to be active
            from daytona._async.snapshot import SnapshotState

            for i in range(60):
                snap = await daytona.snapshot.get(args.snapshot_name)
                if snap.state == SnapshotState.ACTIVE:
                    logger.info("Snapshot is active and ready to use!")
                    break
                await asyncio.sleep(5)
                if i % 6 == 0:
                    logger.info(f"Waiting for snapshot... (state={snap.state})")
            else:
                logger.warning("Snapshot may not be fully active yet")

        finally:
            # Clean up the sandbox
            logger.info("Deleting temporary sandbox...")
            await sandbox.delete()
            logger.info("Done!")

    print(f"\nSnapshot ready: {args.snapshot_name}")
    print(f"Use with: --daytona_snapshot_name {args.snapshot_name}")
    print(f"Or set in global_configs.py: daytona_snapshot_name='{args.snapshot_name}'")


if __name__ == "__main__":
    asyncio.run(main())
