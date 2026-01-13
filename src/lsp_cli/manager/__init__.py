from __future__ import annotations

import subprocess
import sys

import httpx

from lsp_cli.settings import MANAGER_CONN_PATH
from lsp_cli.utils.http import HttpClient
from lsp_cli.utils.socket import is_server_alive

from .manager import Manager, get_manager, manager_lifespan
from .models import (
    ConnectionInfo,
    CreateClientRequest,
    CreateClientResponse,
    DeleteClientRequest,
    DeleteClientResponse,
    ManagedClientInfo,
    ManagedClientInfoList,
)

__all__ = [
    "Manager",
    "ManagedClientInfo",
    "ManagedClientInfoList",
    "CreateClientRequest",
    "CreateClientResponse",
    "DeleteClientRequest",
    "DeleteClientResponse",
    "connect_manager",
    "get_manager",
    "manager_lifespan",
]


def connect_manager() -> HttpClient:
    conn = None
    if MANAGER_CONN_PATH.exists():
        try:
            conn = ConnectionInfo.model_validate_json(MANAGER_CONN_PATH.read_text())
        except (OSError, ValueError, Exception):
            # Failed to read or parse connection info - will try to start manager
            # Catches OSError (file read), ValueError (JSON/validation), or other parsing errors
            pass

    if not conn or not is_server_alive(
        uds_path=conn.uds_path, host=conn.host, port=conn.port
    ):
        subprocess.Popen(
            (sys.executable, "-m", "lsp_cli.manager"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Wait for manager.json to be created and server to be alive
        import time

        start = time.time()
        while time.time() - start < 10:
            if MANAGER_CONN_PATH.exists():
                try:
                    conn = ConnectionInfo.model_validate_json(
                        MANAGER_CONN_PATH.read_text()
                    )
                    if is_server_alive(
                        uds_path=conn.uds_path, host=conn.host, port=conn.port
                    ):
                        break
                except (OSError, ValueError, Exception):
                    # Failed to read/parse - retry in next iteration
                    pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Failed to start manager")

    assert conn is not None
    if conn.uds_path:
        transport = httpx.HTTPTransport(uds=str(conn.uds_path), retries=5)
    else:
        transport = httpx.HTTPTransport(retries=5)

    return HttpClient(
        httpx.Client(
            transport=transport,
            base_url=conn.url,
        )
    )
