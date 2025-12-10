from __future__ import annotations

from typing import Any, Optional, Sequence, Iterable

import mcp.types as types
from mcp.server.fastmcp.tools import Tool, ToolManager
from mcp.server.fastmcp.utilities.logging import get_logger
from mcp.server.state.helper.extract_session_id import extract_session_id
from mcp.server.state.machine.state_machine import StateMachine, InputSymbol, SessionScope
from mcp.server.state.types import FastMCPContext, ResultType

logger = get_logger(__name__)


class StateAwareToolManager:
    """State-aware facade over ``ToolManager``.

    Wraps a ``StateMachine`` and delegates to the native manager while constraining
    discovery and invocation by the machine's current state.

    Facade model
    - Discovery via ``state_machine.available_symbols("tool")``.
    - ``state_machine.step(...)`` emits SUCCESS or ERROR around the call.

    Session model: ambient via ``SessionScope(extract_session_id(ctx))`` per call.
    """

    def __init__(
        self,
        state_machine: StateMachine,
        tool_manager: ToolManager,
    ):
        self._tool_manager = tool_manager
        self._state_machine = state_machine

    def _get_and_validate_tool(
        self,
        name: str,
        symbols: Iterable[InputSymbol],
    ) -> Tool:
        """
        Resolve and validate a tool binding in the current state.

        This enforces that
        - at least one symbol with ``ident == name`` exists in the current state
        - the binding covers the full ResultType space (one symbol per variant)
        - the tool is registered in the underlying ToolManager
        """
        state_name = self._state_machine.current_state()

        # All symbols for this tool in the current state
        matching = [s for s in symbols if s.ident == name]

        if not matching:
            raise ValueError(
                f"Tool '{name}' is not allowed in state '{state_name}'. "
                "Use the method tools/list to inspect availability."
            )

        # Check completeness of the result space
        observed_results = {s.result for s in matching}
        expected_results = set(ResultType)

        if observed_results != expected_results:
            raise ValueError(
                "Inconsistent state machine configuration. "
                f"Binding 'tool/{name}' in state '{state_name}' "
                "does not cover the complete result space."
            )

        tool = self._tool_manager.get_tool(name)
        if tool is None:
            raise ValueError(
                f"Tool '{name}' is expected in state '{state_name}' "
                "but is not registered in the ToolManager."
            )

        return tool

    def list_tools(self, ctx: Optional[FastMCPContext] = None) -> list[Tool]:
        """Return all tools that are allowed in the current state."""
        with SessionScope(extract_session_id(ctx)):
            symbols = self._state_machine.get_symbols("tool")
            allowed_names = {s.ident for s in symbols}

            tools: list[Tool] = []
            for name in allowed_names:
                tool = self._get_and_validate_tool(name, symbols)
                tools.append(tool)

            return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: FastMCPContext,
    ) -> Sequence[types.ContentBlock] | dict[str, Any]:
        """Execute a tool in the current state with SUCCESS or ERROR step semantics."""
        with SessionScope(extract_session_id(ctx)):
            symbols = self._state_machine.get_symbols("tool")
            tool = self._get_and_validate_tool(name, symbols)

            # State step scope
            async with self._state_machine.step(kind="tool", ident=name, ctx=ctx):
                return await tool.run(arguments, context=ctx, convert_result=True)