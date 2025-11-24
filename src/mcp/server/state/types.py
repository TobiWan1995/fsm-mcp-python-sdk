from enum import Enum
from typing import Any, Awaitable, Callable, Optional, TypeAlias

from starlette.requests import Request

from mcp.server.fastmcp import Context
from mcp.server.lowlevel.server import LifespanResultT
from mcp.server.session import ServerSession


# Shorter version of Context from FastMCP
FastMCPContext = Context[ServerSession, LifespanResultT, Request]

# Callback functions used by the state machine (e.g., transition hooks).
# May be synchronous or asynchronous. Keep this NON-optional; add `| None` at use sites.
Callback: TypeAlias = Callable[..., Awaitable[Any] | Any] | None

# Resolver function that yields the current request context (or None if unavailable).
# The resolver itself may be absent (None), e.g., when running in a global/non-session scope.
ContextResolver: TypeAlias = Callable[[], Optional[FastMCPContext]] | None

class ResultType(str, Enum):
    """Generic result type shared across tools, prompts, and resources."""
    SUCCESS = "success"
    ERROR = "error"
