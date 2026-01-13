from __future__ import annotations

from pathlib import Path

from lsp_client.jsonrpc.types import RawNotification, RawRequest, RawResponsePackage
from pydantic import BaseModel, RootModel


class ConnectionInfo(BaseModel):
    """Connection information for LSP server communication.

    On Unix-like systems, uses Unix Domain Sockets (uds_path).
    On Windows, uses TCP with host and port.

    Security Note: When using TCP (Windows), the server binds to 127.0.0.1
    (localhost only) without authentication. This means any local process
    can connect to the manager. This is acceptable for a local development
    tool, but should be considered when handling sensitive data.
    """

    uds_path: Path | None = None
    host: str | None = None
    port: int | None = None

    @property
    def url(self) -> str:
        """
        Return the HTTP URL for this connection.

        If both ``host`` and ``port`` are set, this returns
        ``"http://{host}:{port}"``. If either value is missing, this
        falls back to ``"http://localhost"`` (used primarily for UDS connections
        where the actual network address is not applicable).
        """
        if self.host and self.port:
            return f"http://{self.host}:{self.port}"
        return "http://localhost"


class ManagedClientInfo(BaseModel):
    project_path: Path
    language: str
    remaining_time: float

    @classmethod
    def format(cls, data: list[ManagedClientInfo] | ManagedClientInfo) -> str:
        infos = [data] if isinstance(data, ManagedClientInfo) else data
        lines = []
        for info in infos:
            lines.append(
                f"{info.language:<10} {info.project_path} ({info.remaining_time:.1f}s)"
            )
        return "\n".join(lines)


class ManagedClientInfoList(RootModel[list[ManagedClientInfo]]):
    pass


class CreateClientRequest(BaseModel):
    path: Path


class CreateClientResponse(BaseModel):
    conn: ConnectionInfo
    info: ManagedClientInfo


class DeleteClientRequest(BaseModel):
    path: Path


class DeleteClientResponse(BaseModel):
    info: ManagedClientInfo | None


class LspRequest(BaseModel):
    payload: RawRequest


class LspResponse(BaseModel):
    payload: RawResponsePackage


class LspNotification(BaseModel):
    payload: RawNotification
