from __future__ import annotations

import asyncio
import inspect
from typing import Optional, Any

from mcp.server.state.types import Callback, FastMCPContext
from mcp.server.state.helper.inject_ctx import inject_context
from mcp.server.fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)


async def apply_callback_with_context(
    callback: Optional[Callback],
    ctx: Optional[FastMCPContext],
) -> None:
    """
    Apply callback if present.

    - Context is always passed via `inject_context` (which handles None).
    - If the callback returns an awaitable, it is awaited.
    - Any exceptions are caught and logged (with stacktrace).
    - If the callback returns a non-None result, it is logged and then ignored.
    """
    if not callable(callback):
        return

    callback_name = getattr(callback, "__name__", repr(callback))
    logger.debug("Executing callback function '%s'.", callback_name)

    try:
        # May return a plain value or an awaitable
        result: Any = inject_context(callback, ctx)

        if inspect.isawaitable(result):
            result = await result

        if result is not None:
            # Result is intentionally ignored; we just log it if we can.
            logger.info(
                "Callback '%s' produced result (ignored): %r",
                callback_name,
                result,
            )

    except asyncio.CancelledError:
        # Cancellation is usually part of normal shutdown.
        logger.debug("Callback '%s' was cancelled.", callback_name)
    except Exception as exc:
        # We swallow the error but log it (with traceback) for debugging.
        logger.warning(
            "Callback '%s' raised an exception: %s",
            callback_name,
            exc,
            exc_info=True,
        )
