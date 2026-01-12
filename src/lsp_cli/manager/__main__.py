import socket

import uvicorn

from lsp_cli.manager.models import ConnectionInfo
from lsp_cli.settings import IS_WINDOWS, MANAGER_CONN_PATH, MANAGER_UDS_PATH
from lsp_cli.utils.socket import allocate_port

from .manager import app

if __name__ == "__main__":
    if IS_WINDOWS:
        sock, port = allocate_port()
        try:
            conn = ConnectionInfo(host="127.0.0.1", port=port)
            MANAGER_CONN_PATH.parent.mkdir(parents=True, exist_ok=True)
            MANAGER_CONN_PATH.write_text(conn.model_dump_json())
            # Pass the file descriptor to uvicorn to avoid race condition
            uvicorn.run(app, host="127.0.0.1", port=port, fd=sock.fileno())
        finally:
            sock.close()
    else:
        MANAGER_UDS_PATH.unlink(missing_ok=True)
        MANAGER_UDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = ConnectionInfo(uds_path=MANAGER_UDS_PATH)
        MANAGER_CONN_PATH.write_text(conn.model_dump_json())
        uvicorn.run(app, uds=str(MANAGER_UDS_PATH))
