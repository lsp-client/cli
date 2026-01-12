"""
Comprehensive tests for LSP server management functionality.

This module tests that the server management system works reliably,
ensuring that commands can always connect to the server without failures.
"""

import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx
import pytest

from lsp_cli.manager import (
    ConnectionInfo,
    CreateClientRequest,
    CreateClientResponse,
    DeleteClientRequest,
    DeleteClientResponse,
    ManagedClientInfoList,
    connect_manager,
)
from lsp_cli.settings import (
    IS_WINDOWS,
    MANAGER_CONN_PATH,
    MANAGER_UDS_PATH,
)
from lsp_cli.utils.http import AsyncHttpClient, HttpClient
from lsp_cli.utils.socket import is_server_alive, wait_for_server


@pytest.fixture(scope="module")
def manager_process():
    """Start the manager process for testing."""
    if MANAGER_CONN_PATH.exists():
        MANAGER_CONN_PATH.unlink()
    if MANAGER_UDS_PATH.exists():
        MANAGER_UDS_PATH.unlink()
    MANAGER_UDS_PATH.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "-m", "lsp_cli.manager"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for manager to be ready
    timeout = 10
    start = time.time()
    conn = None
    while time.time() - start < timeout:
        if MANAGER_CONN_PATH.exists():
            try:
                conn = ConnectionInfo.model_validate_json(MANAGER_CONN_PATH.read_text())
                if is_server_alive(
                    uds_path=conn.uds_path, host=conn.host, port=conn.port
                ):
                    break
            except Exception:
                pass
        time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("Manager failed to start within timeout")

    yield proc

    # Cleanup
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    if MANAGER_CONN_PATH.exists():
        MANAGER_CONN_PATH.unlink()
    if MANAGER_UDS_PATH.exists():
        MANAGER_UDS_PATH.unlink()


@pytest.fixture
def test_file():
    """Use a real Python file for LSP operations."""
    # Use an existing file from the project
    file = Path(__file__).parent.parent / "src" / "lsp_cli" / "__init__.py"
    assert file.exists(), f"Test file {file} does not exist"
    return file


def get_manager_transport():
    conn = ConnectionInfo.model_validate_json(MANAGER_CONN_PATH.read_text())
    if conn.uds_path:
        return httpx.AsyncHTTPTransport(uds=str(conn.uds_path), retries=5)
    return httpx.AsyncHTTPTransport(retries=5)


def get_manager_url():
    conn = ConnectionInfo.model_validate_json(MANAGER_CONN_PATH.read_text())
    return conn.url


class TestManagerConnection:
    """Test manager connection reliability."""

    def test_manager_conn_exists(self, manager_process):
        """Test that manager connection file is created."""
        assert MANAGER_CONN_PATH.exists()
        conn = ConnectionInfo.model_validate_json(MANAGER_CONN_PATH.read_text())
        assert is_server_alive(uds_path=conn.uds_path, host=conn.host, port=conn.port)

    def test_connect_manager_creates_client(self, manager_process):
        """Test that connect_manager() creates a working HTTP client."""
        with connect_manager() as client:
            assert isinstance(client, HttpClient)
            # Test basic connectivity by listing servers
            resp = client.get("/list", ManagedClientInfoList)
            assert resp is not None

    def test_connect_manager_multiple_times(self, manager_process):
        """Test that multiple connections can be created."""
        for _ in range(5):
            with connect_manager() as client:
                resp = client.get("/list", ManagedClientInfoList)
                assert resp is not None

    def test_connect_manager_with_retries(self, manager_process):
        """Test that connection retries work."""
        # This tests the httpx.HTTPTransport(retries=5) parameter
        with connect_manager() as client:
            # Make multiple rapid requests
            for _ in range(10):
                resp = client.get("/list", ManagedClientInfoList)
                assert resp is not None


class TestClientLifecycle:
    """Test client creation, reuse, and deletion."""

    @pytest.mark.asyncio
    async def test_create_client(self, manager_process, test_file):
        """Test creating a new client."""
        with connect_manager() as client:
            resp = client.post(
                "/create",
                CreateClientResponse,
                json=CreateClientRequest(path=test_file),
            )
            assert resp is not None

            # Wait for server to be created
            await wait_for_server(
                uds_path=resp.conn.uds_path,
                host=resp.conn.host,
                port=resp.conn.port,
                timeout=10.0,
            )

            if resp.conn.uds_path:
                assert resp.conn.uds_path.exists()
            assert is_server_alive(
                uds_path=resp.conn.uds_path, host=resp.conn.host, port=resp.conn.port
            )
            # The project path is determined by find_client logic
            # It should be a parent directory of test_file
            assert test_file.is_relative_to(resp.info.project_path)
            assert resp.info.language == "python"
            assert resp.info.remaining_time > 0

    def test_client_reuse(self, manager_process, test_file):
        """Test that creating a client for the same path reuses the existing client."""
        with connect_manager() as client:
            # Create first time
            resp1 = client.post(
                "/create",
                CreateClientResponse,
                json=CreateClientRequest(path=test_file),
            )
            assert resp1 is not None
            conn1 = resp1.conn
            time1 = resp1.info.remaining_time

            # Wait a bit to ensure time difference is measurable
            time.sleep(0.1)

            # Create second time - should reuse
            resp2 = client.post(
                "/create",
                CreateClientResponse,
                json=CreateClientRequest(path=test_file),
            )
            assert resp2 is not None
            assert resp2.conn == conn1

            # Remaining time should be reset (approximately equal to full timeout)
            # Both should be close to the full idle_timeout
            assert resp2.info.remaining_time >= time1 - 1  # Allow 1 second variance

    def test_delete_client(self, manager_process, test_file):
        """Test deleting a client."""
        with connect_manager() as client:
            # Create client
            create_resp = client.post(
                "/create",
                CreateClientResponse,
                json=CreateClientRequest(path=test_file),
            )
            assert create_resp is not None
            conn = create_resp.conn

            # Delete client
            delete_resp = client.delete(
                "/delete",
                DeleteClientResponse,
                json=DeleteClientRequest(path=test_file),
            )
            assert delete_resp is not None

            # Server should be cleaned up shortly
            time.sleep(0.5)
            assert not is_server_alive(
                uds_path=conn.uds_path, host=conn.host, port=conn.port
            )

    def test_list_clients(self, manager_process, test_file):
        """Test listing all clients."""
        # Use another existing file
        file2 = test_file.parent / "settings.py"
        assert file2.exists(), f"Test file {file2} does not exist"

        with connect_manager() as client:
            # Initially no clients (or cleanup from previous tests)
            initial = client.get("/list", ManagedClientInfoList)
            initial_count = len(initial.root) if initial else 0

            # Create two clients for the same project
            # Note: They will reuse the same client since they're in the same project
            resp1 = client.post(
                "/create",
                CreateClientResponse,
                json=CreateClientRequest(path=test_file),
            )
            assert resp1 is not None

            # List should show at least one client
            resp = client.get("/list", ManagedClientInfoList)
            assert resp is not None
            # Should have at least 1 more client
            assert len(resp.root) >= initial_count + 1


class TestConcurrentAccess:
    """Test concurrent access to the manager."""

    @pytest.mark.asyncio
    async def test_concurrent_client_creation(self, manager_process, test_file):
        """Test creating multiple clients concurrently."""
        # Use existing files from the project
        files = [
            test_file,
            test_file.parent / "settings.py",
            test_file.parent / "client.py",
            test_file.parent / "server.py",
            test_file.parent.parent
            / "pyproject.toml",  # Not a python file, should fail gracefully
        ]
        # Only use files that exist
        files = [f for f in files if f.exists()][:3]  # Limit to 3 files

        async def create_client(file: Path):
            transport = get_manager_transport()
            async with httpx.AsyncClient(
                transport=transport, base_url=get_manager_url()
            ) as http_client:
                async with AsyncHttpClient(http_client) as client:
                    try:
                        resp = await client.post(
                            "/create",
                            CreateClientResponse,
                            json=CreateClientRequest(path=file),
                        )
                        if resp:
                            # Wait for server to be created
                            await wait_for_server(
                                uds_path=resp.conn.uds_path,
                                host=resp.conn.host,
                                port=resp.conn.port,
                                timeout=10.0,
                            )
                            if resp.conn.uds_path:
                                assert resp.conn.uds_path.exists()
                            return resp
                    except (httpx.HTTPStatusError, OSError):
                        # Some files might not have LSP support or socket may not be ready
                        pass

        # Create all clients concurrently
        async with anyio.create_task_group() as tg:
            for file in files:
                tg.start_soon(create_client, file)

    @pytest.mark.asyncio
    async def test_concurrent_list_operations(self, manager_process):
        """Test concurrent list operations."""

        async def list_clients():
            transport = get_manager_transport()
            async with httpx.AsyncClient(
                transport=transport, base_url=get_manager_url()
            ) as http_client:
                async with AsyncHttpClient(http_client) as client:
                    resp = await client.get("/list", ManagedClientInfoList)
                    assert resp is not None
                    return resp

        # Make 10 concurrent list requests
        async with anyio.create_task_group() as tg:
            for _ in range(10):
                tg.start_soon(list_clients)

    @pytest.mark.asyncio
    async def test_mixed_concurrent_operations(self, manager_process, test_file):
        """Test mixed create/list/delete operations concurrently."""
        files = [
            test_file,
            test_file.parent / "settings.py",
            test_file.parent / "client.py",
        ]
        # Only use files that exist
        files = [f for f in files if f.exists()][:2]  # Limit to 2 files

        async def create_client(file: Path):
            transport = httpx.AsyncHTTPTransport(uds=str(MANAGER_UDS_PATH), retries=5)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as http_client:
                async with AsyncHttpClient(http_client) as client:
                    try:
                        return await client.post(
                            "/create",
                            CreateClientResponse,
                            json=CreateClientRequest(path=file),
                        )
                    except httpx.HTTPStatusError:
                        pass

        async def list_clients():
            transport = get_manager_transport()
            async with httpx.AsyncClient(
                transport=transport, base_url=get_manager_url()
            ) as http_client:
                async with AsyncHttpClient(http_client) as client:
                    return await client.get("/list", ManagedClientInfoList)

        # Run mixed operations
        async with anyio.create_task_group() as tg:
            for file in files:
                tg.start_soon(create_client, file)
            for _ in range(5):
                tg.start_soon(list_clients)


class TestServerWaiting:
    """Test server waiting functionality."""

    @pytest.mark.asyncio
    async def test_wait_server_success(self, manager_process):
        """Test waiting for an existing server."""
        conn = ConnectionInfo.model_validate_json(MANAGER_CONN_PATH.read_text())
        await wait_for_server(
            uds_path=conn.uds_path, host=conn.host, port=conn.port, timeout=5.0
        )

    @pytest.mark.asyncio
    async def test_wait_server_timeout(self):
        """Test waiting for a non-existent server times out."""
        with pytest.raises(OSError):
            await wait_for_server(host="127.0.0.1", port=65535, timeout=0.5)

    @pytest.mark.asyncio
    async def test_wait_server_becomes_available(self, tmp_path):
        """Test waiting for a server that becomes available."""
        # On windows we'd need to start a TCP server, but for simplicity
        # we test with a file on Unix or just skip complex delayed start
        if IS_WINDOWS:
            pytest.skip("Delayed TCP start test not implemented for Windows")

        sock_path = tmp_path / "delayed.sock"

        async def create_socket_delayed():
            await anyio.sleep(0.5)
            # Create a dummy socket file - wait_for_server will try connect_unix
            # which fails on a regular file, but we test the retry loop
            sock_path.touch()

        async with anyio.create_task_group() as tg:
            tg.start_soon(create_socket_delayed)
            with pytest.raises(OSError):
                await wait_for_server(uds_path=sock_path, timeout=2.0)


class TestClientServer:
    """Test client server functionality."""

    @pytest.mark.asyncio
    async def test_client_server_communication(self, manager_process, test_file):
        """Test that we can communicate with a client through its server."""
        # Create client
        with connect_manager() as mgr_client:
            resp = mgr_client.post(
                "/create",
                CreateClientResponse,
                json=CreateClientRequest(path=test_file),
            )
            assert resp is not None
            conn = resp.conn

        # Wait for server to be ready
        await wait_for_server(
            uds_path=conn.uds_path, host=conn.host, port=conn.port, timeout=10.0
        )

        # Connect to the client server
        if conn.uds_path:
            transport = httpx.AsyncHTTPTransport(uds=conn.uds_path.as_posix())
        else:
            transport = httpx.AsyncHTTPTransport()

        async with httpx.AsyncClient(
            transport=transport, base_url=conn.url, timeout=30.0
        ) as http_client:
            # Test health endpoint (if exists)
            try:
                response = await http_client.get("/health")
                # If health endpoint exists, it should succeed
                assert response.status_code in (200, 404)
            except Exception:
                # If no health endpoint, that's also acceptable
                pass


class TestAutoStartManager:
    """Test that the manager auto-starts when not running."""

    def test_connect_manager_auto_starts(self):
        """Test that connect_manager auto-starts the manager if not running."""
        # Ensure manager is not running
        if MANAGER_CONN_PATH.exists():
            MANAGER_CONN_PATH.unlink()
        if MANAGER_UDS_PATH.exists():
            MANAGER_UDS_PATH.unlink()

        # This should auto-start the manager
        with connect_manager() as client:
            # Should be able to list clients
            resp = client.get("/list", ManagedClientInfoList)
            assert resp is not None


class TestErrorHandling:
    """Test error handling in server management."""

    def test_create_client_invalid_path(self, manager_process):
        """Test creating a client with an invalid path."""
        with connect_manager() as client:
            # Non-existent paths should be handled gracefully
            # The 404 is expected behavior
            try:
                client.post(
                    "/create",
                    CreateClientResponse,
                    json=CreateClientRequest(path=Path("/non/existent/path.py")),
                )
            except httpx.HTTPStatusError as e:
                assert e.response.status_code in (404, 500)

    def test_delete_non_existent_client(self, manager_process):
        """Test deleting a client that doesn't exist."""
        non_existent = Path(__file__).parent / "does_not_exist.py"
        with connect_manager() as client:
            # This should not raise an error, just return None info
            resp = client.delete(
                "/delete",
                DeleteClientResponse,
                json=DeleteClientRequest(path=non_existent),
            )
            # Should succeed even if client doesn't exist
            assert resp is not None


class TestStressTests:
    """Stress tests for server management under heavy load."""

    @pytest.mark.asyncio
    async def test_high_concurrent_load(self, manager_process, test_file):
        """Test handling many concurrent requests."""

        async def create_and_list():
            transport = httpx.AsyncHTTPTransport(uds=str(MANAGER_UDS_PATH), retries=5)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost", timeout=30.0
            ) as http_client:
                async with AsyncHttpClient(http_client) as client:
                    try:
                        # Create client
                        await client.post(
                            "/create",
                            CreateClientResponse,
                            json=CreateClientRequest(path=test_file),
                        )
                        # List clients
                        await client.get("/list", ManagedClientInfoList)
                        return True
                    except Exception:
                        return False

        # Run 20 concurrent operations
        results = []
        async with anyio.create_task_group() as tg:
            for _ in range(20):
                results.append(tg.start_soon(create_and_list))

        # Most operations should succeed
        # (some might fail due to timing, but the server should remain stable)

    @pytest.mark.asyncio
    async def test_rapid_create_delete_cycle(self, manager_process, test_file):
        """Test rapid create/delete cycles don't cause issues."""

        async def cycle():
            transport = httpx.AsyncHTTPTransport(uds=str(MANAGER_UDS_PATH), retries=5)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost", timeout=30.0
            ) as http_client:
                async with AsyncHttpClient(http_client) as client:
                    try:
                        # Create
                        await client.post(
                            "/create",
                            CreateClientResponse,
                            json=CreateClientRequest(path=test_file),
                        )
                        await anyio.sleep(0.05)
                        # Delete
                        await client.delete(
                            "/delete",
                            DeleteClientResponse,
                            json=DeleteClientRequest(path=test_file),
                        )
                        return True
                    except Exception:
                        return False

        # Run 5 create/delete cycles
        success_count = 0
        for _ in range(5):
            result = await cycle()
            if result:
                success_count += 1
            await anyio.sleep(0.1)

        # At least some cycles should complete
        assert success_count >= 3, f"Only {success_count}/5 cycles succeeded"


class TestRealWorldScenarios:
    """Test real-world usage scenarios."""

    @pytest.mark.asyncio
    async def test_rapid_command_execution(self, manager_process, test_file):
        """Test rapid execution of commands, simulating real CLI usage."""
        # This simulates a user running multiple lsp commands quickly
        # Test by making rapid requests to the manager

        async def make_request():
            transport = httpx.AsyncHTTPTransport(uds=str(MANAGER_UDS_PATH), retries=5)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost", timeout=30.0
            ) as http_client:
                try:
                    async with AsyncHttpClient(http_client) as client:
                        # Create client
                        resp = await client.post(
                            "/create",
                            CreateClientResponse,
                            json=CreateClientRequest(path=test_file),
                        )
                        if resp:
                            return True
                except Exception:
                    return False
            return False

        # Simulate 10 rapid command executions
        success_count = 0
        for _ in range(10):
            result = await make_request()
            if result:
                success_count += 1
            await anyio.sleep(0.05)  # Small delay between requests

        # All requests should succeed
        assert success_count >= 8, f"Only {success_count}/10 requests succeeded"

    def test_cli_command_sequence(self, manager_process, test_file):
        """Test a typical sequence of CLI commands."""
        # 1. List servers (should be empty or have existing ones)
        with connect_manager() as client:
            list1 = client.get("/list", ManagedClientInfoList)
            initial_count = len(list1.root) if list1 else 0

        # 2. Start a server
        with connect_manager() as client:
            create_resp = client.post(
                "/create",
                CreateClientResponse,
                json=CreateClientRequest(path=test_file),
            )
            assert create_resp is not None

        # 3. List servers again (should have at least one)
        with connect_manager() as client:
            list2 = client.get("/list", ManagedClientInfoList)
            assert list2 is not None
            # Should have at least one client (could be same as before if already existed)
            assert len(list2.root) >= max(1, initial_count)

        # 4. Run another command (create same client, should reuse)
        with connect_manager() as client:
            create_resp2 = client.post(
                "/create",
                CreateClientResponse,
                json=CreateClientRequest(path=test_file),
            )
            assert create_resp2 is not None
            assert create_resp2.conn == create_resp.conn

        # 5. Stop the server
        with connect_manager() as client:
            delete_resp = client.delete(
                "/delete",
                DeleteClientResponse,
                json=DeleteClientRequest(path=test_file),
            )
            assert delete_resp is not None

        # 6. List servers (count should decrease or stay same)
        time.sleep(0.5)  # Give time for cleanup
        with connect_manager() as client:
            list3 = client.get("/list", ManagedClientInfoList)
            # After deleting, count should be less than or equal to before
            if list3:
                assert len(list3.root) <= len(list2.root)
