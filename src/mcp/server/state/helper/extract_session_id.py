from __future__ import annotations
from typing import Optional

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.utilities.logging import get_logger
from mcp.server.lowlevel.server import LifespanResultT, ServerSession

logger = get_logger(__name__)


def extract_session_id(
    ctx: Context[ServerSession, LifespanResultT] | None,
) -> Optional[str]:
    """Extract session id from the current request context (headers or query).

    Tries headers 'x-session-id' / 'x-state-id', then query params 'session_id' / 'state_id'.
    Returns None when no request context is available or no id is present.
    """
    if ctx is None:
        return None

    try:
        req = ctx.request_context.request
    except Exception as exc:
        logger.warning(
            "Failed to access request on context for session id extraction: %s",
            exc,
        )
        return None

    # Try headers first
    try:
        h = getattr(req, "headers", None)
        if h:
            v = h.get("x-session-id") or h.get("x-state-id")
            if isinstance(v, str) and v:
                return v
    except Exception as exc:
        logger.warning(
            "Failed to inspect headers for session id extraction: %s",
            exc,
        )

    # Fallback to query params
    try:
        q = getattr(req, "query", None) or getattr(req, "query_params", None)
        if q:
            v = q.get("session_id") or q.get("state_id")
            if isinstance(v, str) and v:
                return v
    except Exception as exc:
        logger.warning(
            "Failed to inspect query parameters for session id extraction: %s",
            exc,
        )

    return None
