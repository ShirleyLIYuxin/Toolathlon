#!/usr/bin/env python3
"""
Prepare a Daytona snapshot with Docker (dockerd) pre-installed for DinD tasks.

The snapshot pre-installs docker.io so that DinD tasks don't need to
apt-get install at runtime (~10s saved). Service images (Poste, Canvas, etc.)
are pulled at runtime by ServiceDeployer since Docker image pull requires
runtime privileges that aren't available during Dockerfile build.

Usage:
    uv run python scripts/prepare_daytona_snapshot.py \
        --snapshot_name toolathlon-dind-v1

Must be run from the Toolathlon repo root directory.
"""

import argparse
import asyncio
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def find_project_root() -> str:
    """Find Toolathlon project root by looking for pyproject.toml."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.dirname(script_dir)
    if os.path.isfile(os.path.join(candidate, "pyproject.toml")):
        return candidate
    if os.path.isfile(os.path.join(os.getcwd(), "pyproject.toml")):
        return os.getcwd()
    return os.getcwd()


async def run_in_sandbox(sandbox, cmd: str, timeout: int = 300, session_id: str = "main") -> tuple[str, str, int]:
    """Execute a command in a persistent sandbox session."""
    from daytona import SessionExecuteRequest

    response = await sandbox.process.execute_session_command(
        session_id,
        SessionExecuteRequest(command=cmd, run_async=True),
        timeout=timeout,
    )
    if response.cmd_id is None:
        raise RuntimeError(f"Cannot find command ID for: {cmd[:80]}")

    deadline = time.time() + timeout
    result = await sandbox.process.get_session_command(session_id, response.cmd_id)
    while result.exit_code is None:
        if time.time() > deadline:
            logger.warning(f"Command timed out after {timeout}s: {cmd[:80]}")
            return ("", f"Timed out after {timeout}s", -1)
        await asyncio.sleep(3)
        result = await sandbox.process.get_session_command(session_id, result.id)

    logs = await sandbox.process.get_session_command_logs(session_id, response.cmd_id)
    return (logs.stdout or "", logs.stderr or "", int(result.exit_code))


async def main():
    parser = argparse.ArgumentParser(description="Prepare Daytona DinD snapshot")
    parser.add_argument(
        "--snapshot_name", required=True,
        help="Name for the snapshot (e.g., toolathlon-dind-v1)",
    )
    parser.add_argument(
        "--base_image", default="lockon0927/toolathlon-task-image:1016beta",
        help="Base Docker image",
    )
    parser.add_argument("--cpu", type=int, default=4, help="CPU cores")
    parser.add_argument("--memory", type=int, default=8, help="Memory in GB")
    parser.add_argument("--disk", type=int, default=10, help="Disk in GB")
    args = parser.parse_args()

    try:
        from daytona import (
            AsyncDaytona,
            CreateSnapshotParams,
            CreateSandboxFromSnapshotParams,
            Image,
            Resources,
        )
    except ImportError:
        print("Error: daytona package not installed")
        sys.exit(1)

    # Ensure DAYTONA_API_KEY
    if not os.environ.get("DAYTONA_API_KEY"):
        project_root = find_project_root()
        configs_dir = os.path.join(project_root, "configs")
        if os.path.isdir(configs_dir):
            sys.path.insert(0, configs_dir)
        try:
            from global_configs import global_configs
            api_key = global_configs.get("daytona_api_key", "")
            if api_key:
                os.environ["DAYTONA_API_KEY"] = api_key
        except Exception as e:
            logger.debug(f"Could not load global_configs: {e}")
    if not os.environ.get("DAYTONA_API_KEY"):
        print("Error: DAYTONA_API_KEY not set and not found in global_configs")
        sys.exit(1)

    # Build image with dockerd pre-installed
    # NOTE: We do NOT pull service images here because Docker build doesn't have
    # the kernel capabilities needed for DinD (unshare/iptables). Service images
    # are pulled at runtime by ServiceDeployer (~15s per image in Daytona sandbox).
    image = (
        Image.base(args.base_image)
        .dockerfile_commands([
            "RUN apt-get update && apt-get install -y docker.io jq && rm -rf /var/lib/apt/lists/*",
            "RUN mkdir -p /workspace/dumps /workspace/logs /workspace/tasks /workspace/deployment /workspace/configs /workspace/.decoupled_runtime",
        ])
    )

    logger.info(f"Dockerfile:\n{image.dockerfile()}")

    # Delete existing snapshot if present
    async with AsyncDaytona() as daytona:
        try:
            existing = await daytona.snapshot.get(args.snapshot_name)
            logger.info(f"Deleting existing snapshot '{args.snapshot_name}' (state={existing.state})")
            await daytona.snapshot.delete(existing)
            logger.info("Waiting for deletion to propagate...")
            for _ in range(30):
                await asyncio.sleep(3)
                try:
                    await daytona.snapshot.get(args.snapshot_name)
                except Exception:
                    break  # deleted
            else:
                logger.warning("Snapshot still exists after 90s, proceeding anyway")
        except Exception:
            pass

        # Create snapshot
        logger.info(f"Creating snapshot '{args.snapshot_name}'...")
        params = CreateSnapshotParams(
            name=args.snapshot_name,
            image=image,
            resources=Resources(cpu=args.cpu, memory=args.memory, disk=args.disk),
        )

        def on_build_log(line: str):
            logger.info(f"[build] {line.rstrip()}")

        snapshot = await daytona.snapshot.create(params, on_logs=on_build_log, timeout=0)
        logger.info(f"Snapshot build initiated: {args.snapshot_name}")

        # Wait for ACTIVE
        from daytona._async.snapshot import SnapshotState
        for i in range(120):
            snap = await daytona.snapshot.get(args.snapshot_name)
            if snap.state == SnapshotState.ACTIVE:
                logger.info("Snapshot is ACTIVE!")
                break
            if snap.state in (SnapshotState.BUILD_FAILED, SnapshotState.ERROR):
                logger.error(f"Snapshot build FAILED: state={snap.state}")
                sys.exit(1)
            await asyncio.sleep(5)
            if i % 12 == 0:
                logger.info(f"Waiting for snapshot... (state={snap.state}, {i*5}s)")
        else:
            logger.warning("Snapshot not active after 600s")
            sys.exit(1)

        # Verify: create sandbox from snapshot, check dockerd works
        logger.info("Verifying snapshot...")
        test_params = CreateSandboxFromSnapshotParams(
            snapshot=args.snapshot_name,
            auto_stop_interval=0,
            auto_delete_interval=0,
            labels={"project": "toolathlon-snapshot-verify"},
        )
        test_sb = await daytona.create(params=test_params, timeout=300)
        try:
            # Start dockerd in one session, verify in another
            daemon_sid = "verify-daemon"
            verify_sid = "verify-main"
            await test_sb.process.create_session(daemon_sid)
            await test_sb.process.create_session(verify_sid)

            # Start dockerd (will timeout from polling, expected)
            await run_in_sandbox(
                test_sb, "dockerd --storage-driver=overlay2 > /var/log/dockerd.log 2>&1",
                timeout=5, session_id=daemon_sid,
            )

            # Wait for ready
            for j in range(20):
                _, _, rc = await run_in_sandbox(
                    test_sb, "docker info > /dev/null 2>&1", timeout=10, session_id=verify_sid,
                )
                if rc == 0:
                    break
                await asyncio.sleep(2)
            else:
                stdout, _, _ = await run_in_sandbox(
                    test_sb, "cat /var/log/dockerd.log | tail -10", timeout=10, session_id=verify_sid,
                )
                logger.error(f"dockerd failed to start:\n{stdout}")
                sys.exit(1)

            logger.info("dockerd started successfully in snapshot sandbox")

            # Verify which dockerd
            stdout, _, _ = await run_in_sandbox(
                test_sb, "which dockerd && dockerd --version", timeout=10, session_id=verify_sid,
            )
            logger.info(f"dockerd: {stdout.strip()}")

            logger.info("Snapshot verification PASSED!")
        finally:
            await test_sb.delete()
            logger.info("Test sandbox deleted")

    print(f"\nSnapshot ready: {args.snapshot_name}")
    print(f"Use with: --daytona_snapshot_name {args.snapshot_name}")
    print(f"Or set in global_configs.py: daytona_snapshot_name='{args.snapshot_name}'")
    print(f"\nNote: Service images (Poste, Canvas, etc.) are pulled at runtime by ServiceDeployer.")
    print(f"First DinD task run will take ~15-30s extra for image pull.")


if __name__ == "__main__":
    asyncio.run(main())
