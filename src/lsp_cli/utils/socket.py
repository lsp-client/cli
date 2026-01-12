import socket
from pathlib import Path

import anyio
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed


def allocate_port() -> tuple[socket.socket, int]:
    """Allocate a free TCP port by binding to port 0.

    Returns a tuple of (socket, port). The socket is kept open and should be
    passed to uvicorn via the `fd` parameter to avoid a race condition where
    another process could bind to the same port between closing the socket
    and uvicorn starting.

    Note: There is still a small race condition if the socket is closed before
    uvicorn binds to it. The recommended pattern is to pass the socket's file
    descriptor directly to uvicorn using `fd=socket.fileno()`, but this may not
    work on all platforms. For best reliability, keep the socket open until
    uvicorn has bound to the port.

    Returns:
        tuple[socket.socket, int]: A tuple of (socket object, port number).
            The caller is responsible for closing the socket after uvicorn starts.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    assert isinstance(port, int)
    return s, port


def is_server_alive(
    uds_path: Path | None = None, host: str | None = None, port: int | None = None
) -> bool:
    if uds_path and uds_path.exists():
        try:
            af_unix = getattr(socket, "AF_UNIX", None)
            if af_unix is not None:
                with socket.socket(af_unix, socket.SOCK_STREAM) as s:
                    s.connect(str(uds_path))
                    return True
        except OSError:
            # Connection failed - socket file exists but server is not responding
            pass

    if host and port:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            # Connection failed - server is not listening on this port
            pass

    return False


async def wait_for_server(
    uds_path: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    timeout: float = 10.0,
) -> None:
    """Wait for a server to become available.

    Raises:
        ValueError: If only uds_path is provided on Windows where UDS is not available.
        OSError: If the server does not become ready within the timeout period.
    """
    # Check if we have any valid connection method
    af_unix = getattr(socket, "AF_UNIX", None)
    has_uds_support = af_unix is not None
    has_tcp_info = host and port

    # Fail fast if only UDS is provided but not supported
    if uds_path and not has_uds_support and not has_tcp_info:
        raise ValueError(
            "Unix Domain Sockets are not available on this platform, "
            "but only uds_path was provided without TCP fallback (host/port)"
        )

    async for attempt in AsyncRetrying(
        stop=stop_after_delay(timeout),
        wait=wait_fixed(0.1),
        reraise=True,
    ):
        with attempt:
            if uds_path and has_uds_support:
                try:
                    _ = await anyio.connect_unix(uds_path)
                    return
                except (OSError, RuntimeError):
                    # Connection failed; suppress to try TCP or retry
                    pass
            if host and port:
                try:
                    _ = await anyio.connect_tcp(host, port)
                    return
                except (OSError, RuntimeError):
                    # Connection failed; suppress to retry
                    pass
            raise OSError("Server not ready")
