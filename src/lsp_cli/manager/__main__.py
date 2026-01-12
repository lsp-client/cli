import socket

import uvicorn

from lsp_cli.manager.models import ConnectionInfo
from lsp_cli.settings import IS_WINDOWS, MANAGER_CONN_PATH, MANAGER_UDS_PATH

from .manager import app

if __name__ == "__main__":
    if IS_WINDOWS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            assert isinstance(port, int)
        conn = ConnectionInfo(host="127.0.0.1", port=port)
        MANAGER_CONN_PATH.write_text(conn.model_dump_json())
        uvicorn.run(app, host="127.0.0.1", port=port)
    else:
        MANAGER_UDS_PATH.unlink(missing_ok=True)
        MANAGER_UDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = ConnectionInfo(uds_path=MANAGER_UDS_PATH)
        MANAGER_CONN_PATH.write_text(conn.model_dump_json())
        uvicorn.run(app, uds=str(MANAGER_UDS_PATH))
