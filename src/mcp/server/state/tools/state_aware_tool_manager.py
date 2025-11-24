from __future__ import annotations

from typing import Any, Optional, Sequence

import mcp.types as types
from mcp.server.fastmcp.tools import Tool, ToolManager
from mcp.server.fastmcp.utilities.logging import get_logger
from mcp.server.state.helper.extract_session_id import extract_session_id
from mcp.server.state.machine.state_machine import StateMachine, SessionScope
from mcp.server.state.types import FastMCPContext

logger = get_logger(__name__)


def _sid(ctx: Optional[FastMCPContext]) -> Optional[str]:
    try:
        return extract_session_id(ctx) if ctx is not None else None
    except Exception:
        return None


class StateAwareToolManager:
    """State-aware **facade** over ``ToolManager``.

    Wraps a ``StateMachine`` and delegates to the native manager while constraining
    discovery/invocation by the machine's *current state*.

    Facade model:
    - Discovery via ``state_machine.available_symbols('tool')`` (names).
    - `state_machine.step(...)` emits SUCCESS/ERROR around the call. Edge effects are best-effort.

    Session model: ambient via ``SessionScope(_sid(ctx))`` per call.
    """

    def __init__(
        self,
        state_machine: StateMachine,
        tool_manager: ToolManager,
    ):
        self._tool_manager = tool_manager
        self._state_machine = state_machine

    def list_tools(self, ctx: Optional[FastMCPContext] = None) -> list[Tool]:
        """Return tools allowed in the **current state** (names via ``available_symbols('tool')``)."""
        with SessionScope(_sid(ctx)):
            allowed_names = self._state_machine.available_symbols("tool")  # Set[str]
            out: list[Tool] = []
            for name in allowed_names:
                tool = self._tool_manager.get_tool(name)
                if tool:
                    out.append(tool)
                else:
                    logger.warning(
                        "Tool '%s' expected in state '%s' but not registered.",
                        name,
                        self._state_machine.current_state(),
                    )
            return out

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: FastMCPContext,
    ) -> Sequence[types.ContentBlock] | dict[str, Any]:
        """Execute the tool in the **current state** with SUCCESS/ERROR step semantics."""
        with SessionScope(_sid(ctx)):
            allowed = self._state_machine.available_symbols("tool")
            if name not in allowed:
                raise ValueError(
                    f"Tool '{name}' is not allowed in state '{self._state_machine.current_state()}'. "
                    f"Use list_tools() to inspect availability."
                )

            tool = self._tool_manager.get_tool(name)
            if not tool:
                raise ValueError(f"Tool '{name}' not found.")

            # State step scope
            async with self._state_machine.step(kind="tool", ident=name, ctx=ctx):
                return await tool.run(arguments, context=ctx, convert_result=True)
