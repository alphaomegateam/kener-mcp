"""Supply the Kener API key: a fixed value, or the output of a helper command.

The helper command runs on first use, not at server start, so a slow secret
manager does not block the MCP handshake. The key is kept in memory only and
is never logged or put in an error message.
"""

import asyncio
import shlex

DEFAULT_CMD_TIMEOUT = 30.0


class ApiKeyError(Exception):
    """The API key could not be obtained."""


class ApiKeyProvider:
    def __init__(
        self,
        api_key: str | None = None,
        api_key_cmd: str | None = None,
        timeout: float = DEFAULT_CMD_TIMEOUT,
    ) -> None:
        if bool(api_key) == bool(api_key_cmd):
            raise ValueError("Provide exactly one of api_key or api_key_cmd.")
        self._static = api_key
        self._argv = shlex.split(api_key_cmd) if api_key_cmd else []
        if api_key_cmd and not self._argv:
            raise ValueError("api_key_cmd is empty.")
        self._timeout = timeout
        self._cached: str | None = None
        self._lock = asyncio.Lock()

    @property
    def uses_command(self) -> bool:
        return not self._static

    async def get(self) -> str:
        if self._static:
            return self._static
        async with self._lock:
            if self._cached is None:
                self._cached = await self._run()
            return self._cached

    def invalidate(self) -> None:
        """Forget a cached key so the next call runs the command again."""
        self._cached = None

    async def _run(self) -> str:
        name = self._argv[0]
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise ApiKeyError(f"Could not run the API key command ({name}): {e.strerror}") from None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), self._timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ApiKeyError(
                f"The API key command ({name}) did not finish in {self._timeout:g}s."
            ) from None
        if proc.returncode != 0:
            # stderr only: stdout could hold part of a secret.
            detail = stderr.decode(errors="replace").strip()[:300]
            raise ApiKeyError(
                f"The API key command ({name}) failed with exit code {proc.returncode}"
                + (f": {detail}" if detail else ".")
            )
        key = stdout.decode(errors="replace").strip()
        if not key:
            raise ApiKeyError(f"The API key command ({name}) printed nothing.")
        if any(c.isspace() for c in key):
            raise ApiKeyError(
                f"The API key command ({name}) printed more than one value. "
                "It must print only the key."
            )
        return key
