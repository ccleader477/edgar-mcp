"""Open an in-memory MCP client session against the server (same task as the test)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import pytest
import respx
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from edgar_mcp import server
from edgar_mcp.edgar import EdgarClient
from edgar_mcp.permissions import Guard, Policy

Opener = Callable[..., AbstractAsyncContextManager[ClientSession]]


@pytest.fixture
def opener(client: EdgarClient, sec_mock: respx.MockRouter, tmp_path: Path) -> Opener:
    @asynccontextmanager
    async def open_session(
        role: str = "analyst", policy: Policy | None = None
    ) -> AsyncIterator[ClientSession]:
        guard = Guard(policy or Policy.load(), role=role, audit_path=tmp_path / "audit.log.jsonl")
        server.configure(client, guard)
        try:
            async with create_connected_server_and_client_session(server.mcp._mcp_server) as s:
                yield s
        finally:
            server.configure(None, None)

    return open_session
