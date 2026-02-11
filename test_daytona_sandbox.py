"""
Temporary test script for DaytonaSandboxExecutor lifecycle.
Tests: start, exec, upload/download file, uv availability, stop.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Ensure configs are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'configs'))

from utils.sandbox.base_executor import SandboxConfig
from utils.sandbox.daytona_executor import DaytonaSandboxExecutor


async def run_tests():
    # Load API key from global_configs
    try:
        from global_configs import global_configs
        api_key = global_configs.get('daytona_api_key', '')
        if api_key and not os.environ.get('DAYTONA_API_KEY'):
            os.environ['DAYTONA_API_KEY'] = api_key
    except Exception as e:
        print(f"Warning: Could not load global_configs: {e}")

    config = SandboxConfig(
        task_dir="finalpool/paper-checker",
        model_short_name="test-model",
        provider="unified",
        max_steps=10,
        timeout_sec=300,
        image_name="lockon0927/toolathlon-task-image:1016beta",
        daytona_cpu=2,
        daytona_memory_gb=4,
        daytona_disk_gb=10,
    )

    executor = DaytonaSandboxExecutor(config)
    passed = 0
    failed = 0

    # Test 1: start()
    print("Test 1: start() - Creating sandbox...")
    try:
        await executor.start()
        print("  PASSED: Sandbox started successfully")
        passed += 1
    except Exception as e:
        print(f"  FAILED: {e}")
        failed += 1
        print(f"\nResults: {passed} passed, {failed} failed")
        return

    try:
        # Test 2: exec()
        print("\nTest 2: exec() - Running command...")
        try:
            result = await executor.exec("echo hello && python3 --version")
            assert result.return_code == 0, f"Expected return code 0, got {result.return_code}"
            assert "hello" in result.stdout, f"Expected 'hello' in stdout, got: {result.stdout}"
            print(f"  PASSED: Output = {result.stdout.strip()}")
            passed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            failed += 1

        # Test 3: upload_file() / download_file()
        print("\nTest 3: upload_file() / download_file() - File transfer...")
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                f.write("test content from toolathlon")
                upload_src = Path(f.name)

            await executor.upload_file(upload_src, "/workspace/test_upload.txt")

            # Verify upload
            verify = await executor.exec("cat /workspace/test_upload.txt")
            assert "test content from toolathlon" in verify.stdout, f"Upload verification failed: {verify.stdout}"

            # Download
            download_dst = Path(tempfile.mktemp(suffix='.txt'))
            await executor.download_file("/workspace/test_upload.txt", download_dst)
            content = download_dst.read_text()
            assert "test content from toolathlon" in content, f"Download verification failed: {content}"

            print(f"  PASSED: Upload and download verified")
            passed += 1

            # Cleanup temp files
            upload_src.unlink(missing_ok=True)
            download_dst.unlink(missing_ok=True)
        except Exception as e:
            print(f"  FAILED: {e}")
            failed += 1

        # Test 4: uv availability
        print("\nTest 4: exec('which uv') - Check uv availability...")
        try:
            result = await executor.exec("which uv")
            if result.return_code == 0:
                print(f"  PASSED: uv found at {result.stdout.strip()}")
                passed += 1
            else:
                print(f"  FAILED: uv not found (exit code {result.return_code})")
                failed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            failed += 1

    finally:
        # Test 5: stop()
        print("\nTest 5: stop() - Cleaning up sandbox...")
        try:
            await executor.stop()
            print("  PASSED: Sandbox deleted successfully")
            passed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed out of 5 tests")
    print(f"{'='*40}")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_tests())
