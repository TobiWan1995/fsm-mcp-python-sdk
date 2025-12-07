from __future__ import annotations

from typing import Any, Optional, Iterable

import pydantic_core

from mcp.types import GetPromptResult
from mcp.server.fastmcp.prompts import Prompt, PromptManager
from mcp.server.fastmcp.utilities.logging import get_logger
from mcp.server.state.helper.extract_session_id import extract_session_id
from mcp.server.state.machine.state_machine import StateMachine, SessionScope, InputSymbol
from mcp.server.state.types import FastMCPContext, ResultType

logger = get_logger(__name__)


class StateAwarePromptManager:
    """State-aware facade over ``PromptManager``.

    Wraps a ``StateMachine`` and delegates to the native manager while constraining
    discovery and rendering by the machine's current state.

    Facade model
    - Discovery via ``state_machine.get_symbols("prompt")``.
    - ``state_machine.step(...)`` emits SUCCESS or ERROR around render. Edge effects are best-effort.

    Session model:  ambient via ``SessionScope(extract_session_id(ctx))``.
    """

    def __init__(
        self,
        state_machine: StateMachine,
        prompt_manager: PromptManager,
    ):
        self._prompt_manager = prompt_manager
        self._state_machine = state_machine

    def _get_and_validate_prompt(
        self,
        name: str,
        symbols: Iterable[InputSymbol],
    ) -> Prompt:
        """
        Resolve and validate a prompt binding in the current state.

        This enforces that
        - at least one symbol with ``ident == name`` exists in the current state
        - the binding covers the full ResultType space (one symbol per variant)
        - the prompt is registered in the underlying PromptManager
        """
        state_name = self._state_machine.current_state()

        # 1) Ensure the DFA actually exposes this prompt in the current state.
        matching = [s for s in symbols if s.ident == name]
        if not matching:
            raise ValueError(
                f"Prompt '{name}' is not allowed in state '{state_name}'. "
                "Use list_prompts() to inspect availability."
            )

        # 2) Check completeness of the result space.
        observed_results = {s.result for s in matching}
        expected_results = set(ResultType)

        if observed_results != expected_results:
            raise ValueError(
                "Inconsistent state machine configuration. "
                f"Binding 'prompt/{name}' in state '{state_name}' "
                "does not cover the complete result space."
            )

        # 3) Ensure the prompt is actually registered.
        prompt = self._prompt_manager.get_prompt(name)
        if prompt is None:
            raise ValueError(
                f"Prompt '{name}' is expected in state '{state_name}' "
                "but is not registered."
            )

        return prompt

    def list_prompts(self, ctx: Optional[FastMCPContext] = None) -> list[Prompt]:
        """Return prompts that are allowed in the current state."""
        with SessionScope(extract_session_id(ctx)):
            symbols = self._state_machine.get_symbols("prompt")
            allowed_names = {s.ident for s in symbols}

            out: list[Prompt] = []
            for name in allowed_names:
                prompt = self._get_and_validate_prompt(name, symbols)
                out.append(prompt)

            return out

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: FastMCPContext,
    ) -> GetPromptResult:
        """Render a prompt in the current state with SUCCESS/ERROR step semantics."""
        with SessionScope(extract_session_id(ctx)):
            symbols = self._state_machine.get_symbols("prompt")
            prompt = self._get_and_validate_prompt(name, symbols)

            # State step scope
            async with self._state_machine.step(kind="prompt", ident=name, ctx=ctx):
                messages = await prompt.render(arguments, context=ctx)
                return GetPromptResult(
                    description=prompt.description,
                    messages=pydantic_core.to_jsonable_python(messages),
                )
