"""Streamable HTTP transport for the MCP Yahoo Finance server.

Exposes the existing MCP ``server`` at ``/mcp`` using the MCP SDK's
Streamable HTTP session manager (no per-request subprocess).
"""

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from mcp_yfinance.server import server

_state: dict[str, StreamableHTTPSessionManager] = {}


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    manager = StreamableHTTPSessionManager(app=server)
    _state["manager"] = manager
    async with manager.run():
        yield
    _state.pop("manager", None)


class _MCPEndpoint:
    """ASGI endpoint delegating to the MCP session manager."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await _state["manager"].handle_request(scope, receive, send)


app = FastAPI(title="mcp-yfinance", lifespan=lifespan)
_endpoint = _MCPEndpoint()
app.router.routes.append(Route("/mcp", endpoint=_endpoint))
app.router.routes.append(Route("/mcp/", endpoint=_endpoint))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
