import socket
from pathlib import Path

import anyio
import httpx
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed


def is_server_alive(
    uds_path: Path | None = None, host: str | None = None, port: int | None = None
) -> bool:
    if uds_path and uds_path.exists():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(str(uds_path))
                return True
        except (OSError, AttributeError):
            # AttributeError if AF_UNIX is not available (Windows)
            pass

    if host and port:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            pass

    return False


async def wait_for_server(
    uds_path: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    timeout: float = 10.0,
) -> None:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(timeout),
        wait=wait_fixed(0.1),
        reraise=True,
    ):
        with attempt:
            if uds_path:
                try:
                    _ = await anyio.connect_unix(uds_path)
                    return
                except (OSError, RuntimeError, AttributeError):
                    pass
            if host and port:
                try:
                    _ = await anyio.connect_tcp(host, port)
                    return
                except (OSError, RuntimeError):
                    pass
            raise OSError("Server not ready")
