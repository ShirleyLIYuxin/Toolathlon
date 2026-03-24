#!/usr/bin/env python3
"""
Minimal test: verify SSE connectivity through Daytona preview URL.

Steps:
1. Create a Daytona sandbox
2. Start a tiny HTTP server with /health and /sse endpoints inside it
3. Get preview URL via sandbox.get_preview_link(port)
4. From the host, hit /health and attempt an SSE connection to /sse
5. Clean up

Usage:
    uv run python scripts/test_daytona_sse.py [--port 10086] [--target us]
"""

import argparse
import asyncio
import logging
import sys
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Minimal Python HTTP server code to run inside the sandbox
MINI_SERVER_CODE = r'''
import asyncio, json
from aiohttp import web
from aiohttp_sse import sse_response

async def handle_health(request):
    return web.json_response({"ok": True, "message": "hello from sandbox"})

async def handle_sse(request):
    async with sse_response(request) as resp:
        await resp.send("/messages/?session_id=test123", event="endpoint")
        # Keep alive for 60s
        for i in range(60):
            await asyncio.sleep(1)
            await resp.send(json.dumps({"tick": i}), event="message")
    return resp

app = web.Application()
app.router.add_get("/health", handle_health)
app.router.add_get("/sse", handle_sse)
web.run_app(app, host="0.0.0.0", port=PORT_PLACEHOLDER)
'''


async def main():
    parser = argparse.ArgumentParser(description="Test Daytona SSE connectivity")
    parser.add_argument("--port", type=int, default=10086)
    parser.add_argument("--target", default="us")
    args = parser.parse_args()

    try:
        from daytona import (
            AsyncDaytona,
            CreateSandboxFromImageParams,
            Image,
            Resources,
        )
    except ImportError:
        print("Error: daytona package not installed")
        sys.exit(1)

    # Ensure DAYTONA_API_KEY is set
    if not os.environ.get("DAYTONA_API_KEY"):
        try:
            sys.path.insert(0, os.path.join(os.getcwd(), "configs"))
            from global_configs import global_configs
            api_key = global_configs.get("daytona_api_key", "")
            if api_key:
                os.environ["DAYTONA_API_KEY"] = api_key
        except Exception:
            pass

    if not os.environ.get("DAYTONA_API_KEY"):
        print("Error: DAYTONA_API_KEY not set and not found in global_configs")
        sys.exit(1)

    sandbox = None
    async with AsyncDaytona() as daytona:
        try:
            # Step 1: Create sandbox
            logger.info("Creating sandbox...")
            params = CreateSandboxFromImageParams(
                image=Image.base("lockon0927/toolathlon-task-image:1016beta"),
                resources=Resources(cpu=2, memory=4, disk=10),
                auto_stop_interval=0,
                auto_delete_interval=0,
                labels={"project": "toolathlon-sse-test"},
            )
            sandbox = await daytona.create(params=params, timeout=300)
            logger.info(f"Sandbox created: {sandbox.id}")

            # Step 2: Write and start mini server
            server_code = MINI_SERVER_CODE.replace("PORT_PLACEHOLDER", str(args.port))

            # Upload server code
            from daytona import FileUpload
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                f.write(server_code)
                tmp_path = f.name

            await sandbox.fs.upload_file(tmp_path, "/tmp/mini_server.py")
            os.unlink(tmp_path)

            # Start server in background
            from uuid import uuid4
            from daytona import SessionExecuteRequest

            session_id = str(uuid4())
            await sandbox.process.create_session(session_id)

            start_cmd = f"nohup uv run python /tmp/mini_server.py > /tmp/server.log 2>&1 & echo $!"
            response = await sandbox.process.execute_session_command(
                session_id,
                SessionExecuteRequest(command=start_cmd, run_async=True),
                timeout=30,
            )
            # Wait for command to finish (it returns the PID)
            if response.cmd_id:
                cmd_result = await sandbox.process.get_session_command(session_id, response.cmd_id)
                while cmd_result.exit_code is None:
                    await asyncio.sleep(1)
                    cmd_result = await sandbox.process.get_session_command(session_id, cmd_result.id)
                logs = await sandbox.process.get_session_command_logs(session_id, response.cmd_id)
                pid = (logs.stdout or "").strip()
                logger.info(f"Mini server started, PID: {pid}")

            # Wait a moment for server to bind
            await asyncio.sleep(3)

            # Step 3: Get signed preview URL (includes auth token in subdomain)
            logger.info(f"Getting signed preview URL for port {args.port}...")
            preview_result = await sandbox.create_signed_preview_url(args.port, expires_in_seconds=3600)
            if not preview_result:
                logger.error("create_signed_preview_url returned empty!")
                # Check server logs
                s2 = str(uuid4())
                await sandbox.process.create_session(s2)
                r2 = await sandbox.process.execute_session_command(
                    s2, SessionExecuteRequest(command="cat /tmp/server.log", run_async=True), timeout=10,
                )
                if r2.cmd_id:
                    cr = await sandbox.process.get_session_command(s2, r2.cmd_id)
                    while cr.exit_code is None:
                        await asyncio.sleep(1)
                        cr = await sandbox.process.get_session_command(s2, cr.id)
                    lg = await sandbox.process.get_session_command_logs(s2, r2.cmd_id)
                    logger.error(f"Server log: {lg.stdout}")
                sys.exit(1)

            # Extract URL string from result object
            if hasattr(preview_result, 'url'):
                preview_url = preview_result.url
            elif isinstance(preview_result, dict) and 'url' in preview_result:
                preview_url = preview_result['url']
            elif isinstance(preview_result, str):
                preview_url = preview_result
            else:
                preview_url = str(preview_result)
            if not preview_url.startswith("http"):
                preview_url = f"https://{preview_url}"

            logger.info(f"Preview URL: {preview_url}")

            # Step 4: Test /health from host
            logger.info("Testing /health endpoint...")
            health_url = f"{preview_url}/health"

            health_ok = False
            for attempt in range(10):
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-sf", "--max-time", "10", health_url,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    logger.info(f"✅ /health OK: {stdout.decode().strip()}")
                    health_ok = True
                    break
                else:
                    logger.info(f"  attempt {attempt+1}/10 failed, retrying... (stderr: {stderr.decode()[:100]})")
                    await asyncio.sleep(3)

            if not health_ok:
                logger.error("❌ /health not reachable through preview URL")
                sys.exit(1)

            # Step 5: Test SSE /sse from host
            logger.info("Testing /sse endpoint (SSE)...")
            sse_url = f"{preview_url}/sse"

            # Use curl with timeout to get first few SSE events
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sf", "--max-time", "15", "-N", sse_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            sse_output = stdout.decode()

            if "endpoint" in sse_output or "event:" in sse_output:
                logger.info(f"✅ SSE works! First bytes:\n{sse_output[:500]}")
            elif sse_output:
                logger.warning(f"⚠️  Got response but no SSE events detected:\n{sse_output[:500]}")
            else:
                logger.error(f"❌ SSE returned empty (stderr: {stderr.decode()[:200]})")
                sys.exit(1)

            # Summary
            print("\n" + "=" * 60)
            print("TEST RESULTS")
            print("=" * 60)
            print(f"  Sandbox ID:   {sandbox.id}")
            print(f"  Preview URL:  {preview_url}")
            print(f"  /health:      {'✅ PASS' if health_ok else '❌ FAIL'}")
            print(f"  /sse (SSE):   {'✅ PASS' if 'endpoint' in sse_output else '⚠️  UNCERTAIN'}")
            print("=" * 60)

        finally:
            if sandbox:
                logger.info("Deleting sandbox...")
                try:
                    await sandbox.delete()
                    logger.info("Sandbox deleted")
                except Exception as e:
                    logger.warning(f"Failed to delete sandbox: {e}")


if __name__ == "__main__":
    asyncio.run(main())
