"""
Singleton manager for the AsyncDaytona client.

Design reference: harbor/src/harbor/environments/daytona.py (DaytonaClientManager)

Ensures a single shared client instance across all DaytonaSandboxExecutor instances,
with proper cleanup at program termination.
"""

import asyncio
import atexit
import logging
from typing import Optional

try:
    from daytona import AsyncDaytona
    DAYTONA_AVAILABLE = True
except ImportError:
    DAYTONA_AVAILABLE = False
    AsyncDaytona = None

logger = logging.getLogger(__name__)


class DaytonaClientManager:
    """
    Singleton manager for the AsyncDaytona client.

    Ensures a single shared client instance across all DaytonaSandboxExecutor instances,
    with proper cleanup at program termination.
    """

    _instance: Optional["DaytonaClientManager"] = None
    _lock = asyncio.Lock()

    def __init__(self):
        if not DAYTONA_AVAILABLE:
            raise ImportError(
                "daytona package is not installed. "
                "Please install it with: pip install daytona"
            )
        self._client: Optional[AsyncDaytona] = None
        self._client_lock = asyncio.Lock()
        self._cleanup_registered = False

    @classmethod
    async def get_instance(cls) -> "DaytonaClientManager":
        """Get or create the singleton instance."""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()

        assert cls._instance is not None
        return cls._instance

    async def get_client(self) -> AsyncDaytona:
        """
        Get the shared AsyncDaytona client, creating it if necessary.

        Returns:
            The shared AsyncDaytona client instance.
        """
        async with self._client_lock:
            if self._client is None:
                logger.debug("Creating new AsyncDaytona client")
                self._client = AsyncDaytona()

                # Register cleanup handler on first client creation
                if not self._cleanup_registered:
                    atexit.register(self._cleanup_sync)
                    self._cleanup_registered = True

            return self._client

    def _cleanup_sync(self):
        """Synchronous cleanup wrapper for atexit."""
        try:
            # Try to get the current event loop
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                # Schedule cleanup in the running loop
                loop.create_task(self._cleanup())
            else:
                # Create a new event loop for cleanup
                asyncio.run(self._cleanup())
        except Exception as e:
            # Use print since logging might not be available during shutdown
            print(f"Error during Daytona client cleanup: {e}")

    async def _cleanup(self):
        """Close the Daytona client if it exists."""
        async with self._client_lock:
            if self._client is not None:
                try:
                    logger.debug("Closing AsyncDaytona client at program exit")
                    await self._client.close()
                    logger.debug("AsyncDaytona client closed successfully")
                except Exception as e:
                    logger.error(f"Error closing AsyncDaytona client: {e}")
                finally:
                    self._client = None

    @classmethod
    async def close(cls):
        """Manually close the client manager and release resources."""
        if cls._instance is not None:
            await cls._instance._cleanup()
            cls._instance = None
