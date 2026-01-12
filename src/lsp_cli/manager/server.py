from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Self, final, override

import httpx
from attrs import define, field
from lsp_client.jsonrpc.types import (
    RawNotification,
    RawRequest,
    RawResponsePackage,
)
from lsp_client.server import Server, ServerRuntimeError
from lsp_client.server.types import ServerRequest
from lsp_client.utils.channel import Sender
from lsp_client.utils.workspace import Workspace

from lsp_cli.manager.models import ConnectionInfo
from lsp_cli.utils.socket import wait_for_server


@final
@define
class ManagerServer(Server):
    conn: ConnectionInfo
    _client: httpx.AsyncClient | None = field(init=False, default=None)

    @property
    def client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client for this server."""
        if self._client is None:
            if self.conn.uds_path:
                transport = httpx.AsyncHTTPTransport(uds=self.conn.uds_path.as_posix())
            else:
                transport = httpx.AsyncHTTPTransport()

            self._client = httpx.AsyncClient(
                transport=transport,
                base_url=self.conn.url,
                timeout=None,
            )
        return self._client

    async def _close_client(self) -> None:
        """Close the HTTP client if it exists."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @override
    async def check_availability(self) -> None:
        if self.conn.uds_path and not self.conn.uds_path.exists():
            raise ServerRuntimeError(
                self, f"Server socket not found: {self.conn.uds_path}"
            )
        try:
            await self.client.get("/health")
        except httpx.HTTPError as e:
            raise ServerRuntimeError(
                self, f"Managed server at {self.conn.url} is not responding: {e}"
            ) from e

    @override
    async def request(self, request: RawRequest) -> RawResponsePackage:
        response = await self.client.post("/request", json=request)
        response.raise_for_status()
        return response.json()

    @override
    async def notify(self, notification: RawNotification) -> None:
        response = await self.client.post("/notify", json=notification)
        response.raise_for_status()

    @override
    async def kill(self) -> None:
        await self.client.post("/shutdown")

    async def wait_requests_completed(self, timeout: float | None = None) -> None:
        return

    @override
    @asynccontextmanager
    async def run(
        self, workspace: Workspace, sender: Sender[ServerRequest]
    ) -> AsyncGenerator[Self]:
        await wait_for_server(
            uds_path=self.conn.uds_path,
            host=self.conn.host,
            port=self.conn.port,
            timeout=10.0,
        )
        try:
            yield self
        finally:
            await self._close_client()
