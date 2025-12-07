from __future__ import annotations
from types import TracebackType
from typing import Callable, Optional, Type, TYPE_CHECKING

from mcp.server.fastmcp.utilities.logging import get_logger
from mcp.server.state.helper.callback import apply_callback_with_context
from mcp.server.state.types import FastMCPContext

if TYPE_CHECKING:  # pragma: no cover
    from mcp.server.state.machine.state_machine import StateMachine, InputSymbol

logger = get_logger(__name__)


class TransitionScope:
    """
    Async context manager that wraps an operation and emits SUCCESS/ERROR transitions.

    Session handling:
      - This scope does **not** bind or resolve sessions.
      - The ambient session (if any) must be set by the caller (e.g. via SessionScope).
      - All StateMachine calls use the current ambient session or fall back to global state.

    Behavior:
      - Looks up an exact transition for the emitted symbol from the current state.
      - If such a transition exists:
          * the state is updated to the edge's `to_state`
          * the edge's effect is executed best-effort (failures are logged only).
      - If no transition exists:
          * an error is logged, because a validated state machine must provide
            a transition for every advertised outcome.
      - After the transition, terminality is evaluated for the symbol-id; if terminal → reset.
      - On the error path, state is updated, terminality is evaluated, then the mapped exception
        is re-raised.
    """

    def __init__(
        self,
        sm: "StateMachine",
        success_symbol: "InputSymbol",
        error_symbol: "InputSymbol",
        *,
        ctx: Optional[FastMCPContext] = None,
        log_exc: Callable[..., None] = logger.exception,
        exc_mapper: Callable[[BaseException], BaseException] = lambda e: ValueError(str(e)),
    ):
        self._sm = sm
        self._success = success_symbol
        self._error = error_symbol
        self._ctx = ctx  # passed to edge effects only
        self._log_exc = log_exc
        self._exc_mapper = exc_mapper

    async def __aenter__(self) -> "TransitionScope":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> Optional[bool]:
        # Decide which symbol to emit based on success/error path
        symbol = self._success if exc_type is None else self._error
        symbol_id = symbol.id  # stable over (type, ident, result)

        # 1) Apply exact transition or fail hard if none exists.
        await self._apply(symbol_id)

        # 2) If the **new** current state is terminal for this symbol-id → reset
        if self._sm.is_terminal(symbol_id):
            self._sm.reset()

        # 3) Re-raise on error path (after state update + potential reset)
        if exc_type is None:
            return False  # do not suppress

        self._log_exc(
            "Exception during execution for symbol '%s/%s' in state '%s'",
            symbol.kind,
            symbol.ident,
            self._sm.current_state(),
        )
        raise self._exc_mapper(exc or RuntimeError("Unknown failure")) from exc

    # ----------------------------
    # internals
    # ----------------------------

    async def _apply(self, symbol_id: str) -> None:
        """
        Apply the exact transition for `symbol_id` from the current state.

        This method assumes that the state machine was built and validated such
        that a transition exists for every advertised input symbol. If no edge
        can be found, this indicates a programming error or an inconsistency
        between the builder/validator and the runtime transition graph.

        In that case, a fatal error is raised instead of silently assuming a
        reflexive self-transition.
        """
        edge = self._sm.get_edge(symbol_id)
        if edge is None:
            cur = self._sm.current_state()
            msg = (
                f"No transition defined for symbol_id={symbol_id!r} in state '{cur}'. "
                "This must not happen for a validated state machine and indicates a "
                "programming error or a mismatch between the builder/validator and "
                "the runtime transition graph."
            )
            logger.error(msg)
        else:
            # Exact match: set next state, then best-effort effect
            self._sm.set_current_state(edge.to_state)
            try:
                await apply_callback_with_context(edge.effect, self._ctx)
            except Exception as e:  # synchronous invocation failures only
                logger.warning(
                    "Transition effect failed (from '%s' -> '%s', symbol_id=%s): %s",
                    edge.from_state,
                    edge.to_state,
                    symbol_id,
                    e,
                )
