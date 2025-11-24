from __future__ import annotations

from typing import Callable, Optional, TypeVar, Literal, Dict, List, Tuple

from mcp.server.fastmcp.prompts import PromptManager
from mcp.server.fastmcp.resources import ResourceManager
from mcp.server.fastmcp.tools import ToolManager
from mcp.server.fastmcp.utilities.logging import get_logger
from mcp.server.state.machine.state_machine import (
    InputSymbol,
    State,
    StateMachine,
    Edge,
)

from mcp.server.state.types import Callback, ResultType
from mcp.server.state.validator import StateMachineValidator, ValidationIssue


logger = get_logger(f"{__name__}.StateMachineBuilder")

# ----------------------------
# Helper Types
# ----------------------------

F = TypeVar("F", bound=Callable[["StateAPI"], None])  # Decorator receives a StateAPI

ResultFactory = Callable[[str, ResultType], InputSymbol]

RESULT_FACTORIES: Dict[str, ResultFactory] = {
    "tool": InputSymbol.for_tool,
    "prompt": InputSymbol.for_prompt,
    "resource": InputSymbol.for_resource,
}

# ----------------------------
# Internal Builder
# ----------------------------

class _InternalStateMachineBuilder:
    """Private, build-only implementation.

    Collects states and edges during DSL usage and produces a state machine.
    Validation is invoked from build methods, never by users directly.
    This class must not be accessed from user code.
    """

    def __init__(
        self,
        tool_manager: ToolManager,
        resource_manager: ResourceManager,
        prompt_manager: PromptManager,
    ):
        """Capture external managers for validation and initialize buffers."""
        self._tool_manager = tool_manager
        self._resource_manager = resource_manager
        self._prompt_manager = prompt_manager

        self._initial: Optional[str] = None
        self._states: Dict[str, State] = {}
        self._edges: List[Edge] = []
        self._symbols_by_id: Dict[str, InputSymbol] = {}

    def add_state(self, name: str, *, is_initial: bool = False) -> None:
        """Declare a state if missing and optionally mark it as initial.

        The first initial declaration wins. Later attempts log a warning and are ignored.
        """
        exists = name in self._states
        if not exists:
            # Create with empty terminal-symbol-id list. Edges are global on the automaton.
            self._states[name] = State(name=name, terminals=[])
        else:
            logger.debug("State '%s' already exists; keeping configuration.", name)

        if is_initial:
            if self._initial is None or self._initial == name:
                self._initial = name
            else:
                logger.warning(
                    "Initial state already set to '%s'. Ignoring attempt to set '%s' as initial.",
                    self._initial,
                    name,
                )

    def _record_symbol(self, symbol: InputSymbol) -> str:
        """Ensure symbol is known to Σ and return its stable id."""
        sid = symbol.id
        existing = self._symbols_by_id.get(sid)
        if existing is None:
            self._symbols_by_id[sid] = symbol
        else:
            # Same id must mean the same triple (type, ident, result) by construction.
            # If this differs, it is a programming error in the builder.
            if (existing.kind, existing.ident, existing.result) != (
                symbol.kind,
                symbol.ident,
                symbol.result,
            ):
                logger.debug("Symbol id collision for %s. Keeping first definition.", sid)
        return sid

    def add_terminal(self, state_name: str, symbol: InputSymbol) -> None:
        """Append a terminal symbol-id to a state's terminal set. Duplicates are ignored."""
        st = self._states.get(state_name)
        if st is None:
            raise KeyError(f"State '{state_name}' not defined")
        sid = self._record_symbol(symbol)
        if sid not in st.terminals:
            st.terminals.append(sid)
        else:
            logger.debug(
                "Terminal symbol-id %s already present on state '%s'. Ignored.",
                sid,
                state_name,
            )

    def add_edge(
        self,
        from_state: str,
        to_state: str,
        symbol: InputSymbol,
        effect: Callback | None = None,
    ) -> None:
        """Add an edge δ(q, a) = q'.

        Behavior:
        - Ensures the target state exists. Flags of existing states are not modified.
        - If a duplicate edge (same from_state, same symbol-id, same to_state) exists,
          a warning is logged and the new edge is ignored.
        - If an edge with the same from_state and symbol-id but a different to_state
          exists, a warning about ambiguity is logged and the new edge is ignored.
        """
        if from_state not in self._states:
            raise KeyError(f"State '{from_state}' not defined")

        # Ensure target state exists as placeholder if needed.
        if to_state not in self._states:
            self.add_state(to_state, is_initial=False)
            logger.debug("Created placeholder state '%s' for edge target.", to_state)

        sid = self._record_symbol(symbol)
        new_edge = Edge(from_state=from_state, to_state=to_state, symbol_id=sid, effect=effect)

        # Duplicate edge
        if any(
            e.from_state == from_state and e.symbol_id == sid and e.to_state == to_state
            for e in self._edges
        ):
            logger.warning("Edge %r already exists. New definition ignored.", new_edge)
            return

        # Ambiguous edge
        if any(
            e.from_state == from_state and e.symbol_id == sid and e.to_state != to_state
            for e in self._edges
        ):
            logger.warning(
                "Ambiguous edge on symbol-id %s from '%s'. Existing target differs. New definition ignored.",
                sid,
                from_state,
            )
            return

        self._edges.append(new_edge)

    def _complete_reflexive_edges(self) -> None:
        """
        Ensure reflexive completion for partially specified SUCCESS/ERROR edges.

        For each binding (from_state, kind, ident) that already has at least one
        edge in the graph, this method materializes missing outcomes as self-loops
        (q, a, q). This implements the reflexive completion δ(q,a) = q for
        unspecified outcomes at build time.
        """
        if not self._edges:
            return

        # (from_state, kind, ident) -> set of present ResultType values
        present: Dict[Tuple[str, str, str], set[ResultType]] = {}

        for e in self._edges:
            sym = self._symbols_by_id.get(e.symbol_id)
            if sym is None:
                # Structural errors (unknown symbol-id) are reported by the validator.
                continue

            kind = sym.kind
            if kind not in RESULT_FACTORIES:
                raise ValueError(f"Unknown symbol kind '{kind}' for edge {e!r} during reflexive completion.")

            key = (e.from_state, kind, sym.ident)
            bucket = present.setdefault(key, set())
            bucket.add(sym.result)

        # For each binding, add missing outcomes as self-loops.
        for (from_state, kind, ident), have_results in present.items():
            factory = RESULT_FACTORIES[kind]
            for result in ResultType:
                if result in have_results:
                    continue  # already specified for this outcome

                symbol = factory(ident, result)
                # Self-loop: from_state -> from_state, no effect.
                self.add_edge(from_state, from_state, symbol, effect=None)
                logger.warning(
                    "Reflexive completion: added self-loop for %s '%s' in state '%s' on result '%s'.",
                    kind,
                    ident,
                    from_state,
                    result.value,
                )

    def build(self) -> StateMachine:
        """Build a global machine with a single current state for the process."""
        # First ensure that every advertised artifact has a complete result space.
        self._complete_reflexive_edges()

        # Then run structural and reference validation.
        self._validate()

        initial = self._initial or next(iter(self._states))
        return StateMachine(
            initial_state=initial,
            states=self._states,
            symbols=list(self._symbols_by_id.values()),
            edges=list(self._edges),
        )

    # ----------------------------
    # Validation
    # ----------------------------

    def _validate(self) -> None:
        """Run structural and reference checks. Errors abort and warnings are logged."""
        issues: List[ValidationIssue] = StateMachineValidator(
            states=self._states,
            edges=self._edges,
            symbols_by_id=self._symbols_by_id,
            initial_state=self._initial,
            tool_manager=self._tool_manager,
            prompt_manager=self._prompt_manager,
            resource_manager=self._resource_manager,
        ).validate()

        for i in issues:
            if i.level == "warning":
                logger.warning("State machine validation warning: %s", i.message)

        errors = [i.message for i in issues if i.level == "error"]
        if errors:
            raise ValueError("Invalid state machine:\n- " + "\n- ".join(errors))

# ----------------------------
# Public API DSL
# ----------------------------

class BaseTransitionAPI:
    """
    Fluent scope for transitions (internally *edges*) of a concrete (kind, name) binding within the current state.

    Outcome-first API:
      - on_success(to_state, *, terminal=False, effect=None) -> Self
      - on_error(to_state,   *, terminal=False, effect=None) -> Self
      - build_edge() -> StateAPI  (return to state scope)

    Effects:
      - `effect` runs *after* the state update when this edge is taken.
      - Effects are non-semantic (logging/metrics/etc.); failures are warned and ignored.

    ResultType for SUCCESS/ERROR is implicitly mapped to on_success/on_error.
    """

    # subclass contract
    _factory: Callable[[str, ResultType], InputSymbol]  # e.g. InputSymbol.for_tool
    _kind: Literal["tool", "prompt", "resource"]

    def __init__(self, builder: _InternalStateMachineBuilder, from_state: str, name: str):
        """Bind the builder, the source state name, and the bound (kind-specific) binding name."""
        self._builder = builder
        self._from = from_state
        self._name = name

    def on_success(
        self,
        to_state: str,
        *,
        terminal: bool = False,
        effect: Optional[Callback] = None,
    ) -> "BaseTransitionAPI":
        """Attach the SUCCESS transition (edge); optionally mark target terminal."""
        symbol = self._factory(self._name, ResultType.SUCCESS)
        self._builder.add_edge(self._from, to_state, symbol, effect)
        if terminal:
            self._builder.add_terminal(to_state, symbol)
        return self

    def on_error(
        self,
        to_state: str,
        *,
        terminal: bool = False,
        effect: Optional[Callback] = None,
    ) -> "BaseTransitionAPI":
        """Attach the ERROR transition (edge); optionally mark target terminal."""
        symbol = self._factory(self._name, ResultType.ERROR)
        self._builder.add_edge(self._from, to_state, symbol, effect)
        if terminal:
            self._builder.add_terminal(to_state, symbol)
        return self

    def build_edge(self) -> "StateAPI":
        """Return to the state scope to continue chaining within the same state."""
        return StateAPI(self._builder, self._from)


class TransitionToolAPI(BaseTransitionAPI):
    """Tool-typed transition scope. Use `on_success`, `on_error`, then `build_tool()` or `build_edge()` to return."""
    _factory        = staticmethod(InputSymbol.for_tool)
    _kind           = "tool"

    def build_tool(self) -> "StateAPI":
        """Return to the state scope to continue attaching bindings for this state."""
        return self.build_edge()


class TransitionPromptAPI(BaseTransitionAPI):
    """Prompt-typed transition scope. Use `on_success`, `on_error`, then `build_prompt()` or `build_edge()` to return."""
    _factory        = staticmethod(InputSymbol.for_prompt)
    _kind           = "prompt"

    def build_prompt(self) -> "StateAPI":
        """Return to the state scope to continue attaching bindings for this state."""
        return self.build_edge()


class TransitionResourceAPI(BaseTransitionAPI):
    """Resource-typed transition scope. Use `on_success`, `on_error`, then `build_resource()` or `build_edge()` to return."""
    _factory        = staticmethod(InputSymbol.for_resource)
    _kind           = "resource"

    def build_resource(self) -> "StateAPI":
        """Return to the state scope to continue attaching bindings for this state."""
        return self.build_edge()


class StateAPI:
    """Fluent scope for a single state (input-first style).

    Entry points (return kind-specific Transition APIs):
      - on_tool(name)     → TransitionToolAPI
      - on_prompt(name)   → TransitionPromptAPI
      - on_resource(name) → TransitionResourceAPI

    To exit the state scope, call `build_state()` to return the DSL facade.
    """

    def __init__(self, builder: _InternalStateMachineBuilder, state_name: str):
        """Bind the internal builder and the current state name for fluent chaining."""
        self._builder = builder
        self._name = state_name

    def on_tool(self, name: str) -> TransitionToolAPI:
        """Attach a tool by name and return a tool-typed Transition API."""
        return TransitionToolAPI(builder=self._builder, from_state=self._name, name=name)

    def on_prompt(self, name: str) -> TransitionPromptAPI:
        """Attach a prompt by name and return a prompt-typed Transition API."""
        return TransitionPromptAPI(builder=self._builder, from_state=self._name, name=name)

    def on_resource(self, name: str) -> TransitionResourceAPI:
        """Attach a resource by name and return a resource-typed Transition API."""
        return TransitionResourceAPI(builder=self._builder, from_state=self._name, name=name)

    def build_state(self) -> "StateMachineDefinition":
        """Return the facade to continue the fluent chain (same builder instance)."""
        return StateMachineDefinition.from_builder(self._builder)


class StateMachineDefinition:
    """Public DSL facade for declaring states and edges.

    Users never call build methods; the server builds and validates at startup.

    **Decorator style**::

        @app.statebuilder.state("start", is_initial=True)
        def _(s: StateAPI):
            s.on_tool("login")
             .on_success("home", terminal=True)
             .build_edge()
             .on_tool("alt_login")
             .on_error("start")
             .build_edge()

    **Fluent style**::

        app.statebuilder
            .define_state("start", is_initial=True)
            .on_prompt("confirm")
                .on_success("end", terminal=True)
                .build_edge()
            .on_tool("help")
                .on_success("faq")
                .build_edge()
    """

    def __init__(
        self,
        tool_manager: ToolManager,
        resource_manager: ResourceManager,
        prompt_manager: PromptManager,
    ):
        """Create a new facade over a fresh internal builder."""
        self._builder = _InternalStateMachineBuilder(tool_manager, resource_manager, prompt_manager)

    @classmethod
    def from_builder(cls, builder: _InternalStateMachineBuilder) -> "StateMachineDefinition":
        """Wrap an existing internal builder (no copy)."""
        obj = cls.__new__(cls)
        obj._builder = builder
        return obj

    def define_state(self, name: str, is_initial: bool = False) -> StateAPI:
        """Declare a state (no update semantics) and return a StateAPI to continue in fluent style."""
        self._builder.add_state(name, is_initial=is_initial)
        return StateAPI(self._builder, name)

    def state(self, name: str, is_initial: bool = False) -> Callable[[F], F]:
        """Decorator for declarative state definition (same semantics as `define_state`)."""
        def decorator(func: F) -> F:
            state_api: StateAPI = self.define_state(name, is_initial)
            func(state_api)
            return func
        return decorator

    def _to_internal_builder(self) -> _InternalStateMachineBuilder:
        """Internal plumbing only (server builds/validates after all registrations)."""
        return self._builder
