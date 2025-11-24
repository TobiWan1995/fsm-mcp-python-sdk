from __future__ import annotations

from typing import Any, Iterable, Literal, Sequence

from pydantic import AnyUrl

from mcp.types import (
    ContentBlock,
    GetPromptResult,
    Prompt as MCPPrompt,
    PromptArgument as MCPPromptArgument,
    Resource as MCPResource,
    Tool as MCPTool,
    ResourceTemplate as MCPResourceTemplate
)

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.logging import get_logger
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.lowlevel.server import LifespanResultT
from mcp.server.state.builder import StateMachineDefinition
from mcp.server.state.machine.state_machine import StateMachine
from mcp.server.state.prompts.state_aware_prompt_manager import StateAwarePromptManager
from mcp.server.state.resources.state_aware_resource_manager import StateAwareResourceManager
from mcp.server.state.tools.state_aware_tool_manager import StateAwareToolManager


logger = get_logger(f"{__name__}.StatefulMCP")

class StatefulMCP(FastMCP[LifespanResultT]):
    """FastMCP with a session-scoped state machine and state-aware managers.

    What it does:
    - Attaches a StateMachine (session-scoped) and routes tool/resource/prompt
      operations through it.
    - Managers list only items allowed in the *current state*.
    - Calls run inside an async transition scope so SUCCESS/ERROR edges fire.

    Overridden handlers:
    - list_tools / call_tool
    - list_resources / read_resource
    - list_prompts / get_prompt

    Session scoping:
    The current state is resolved from the request context (per session). If no
    context is available, a shared fallback state is used.

    Important:
    Define your states via `statebuilder` before `run()`. The graph is built and
    validated at startup; missing/invalid definitions will fail startup.
    """

    def __init__(
            self, 
            *args: Any,
            **kwargs: Any
        ) -> None:
        # Parent initialization sets up _mcp_server and native managers
        super().__init__(*args, **kwargs)

        # Public DSL to define
        self._state_definition = StateMachineDefinition(
            self._tool_manager, self._resource_manager, self._prompt_manager)

        # Session-scoped state machine runtime (built in run())
        self._state_machine: StateMachine | None = None

        # Our state-aware managers (built in run())
        self._stateful_tools: StateAwareToolManager | None = None
        self._stateful_resources: StateAwareResourceManager | None = None
        self._stateful_prompts: StateAwarePromptManager | None = None


    # ----------------------------
    # Public surface
    # ----------------------------

    @property
    def statebuilder(self) -> StateMachineDefinition:
        """Finite-state machine DSL (public).

        Declare states and edges; attach (tool|prompt|resource) bindings with **outcome-specific**
        edges. Use `on_success(...)` / `on_error(...)` to wire edges, optionally passing
        `terminal=` and `effect=`. The server builds & validates the graph
        at startup—do not call internal build methods yourself.

        Decorator style::

            @app.statebuilder.state("start", is_initial=True)
            def _(s: StateAPI):
                s.on_tool("login")
                .on_success("home", terminal=True)
                .build_edge()
                .on_tool("alt_login")
                .on_error("start")
                .build_edge()

        Fluent style::

            app.statebuilder
                .define_state("start", is_initial=True)
                .on_prompt("confirm")
                    .on_success("end", terminal=True)
                    .build_edge()
                .on_tool("help")
                    .on_success("faq")
                    .build_edge()

        """
        return self._state_definition

    # ----------------------------
    # Server lifecycle
    # ----------------------------

    def run(
        self,
        transport: Literal["stdio", "sse", "streamable-http"] = "stdio",
        mount_path: str | None = None,
    ) -> None:
        """Run the server. Build state machine and initialize state-aware managers once."""
        self._build_state_machine()
        self._init_state_aware_managers()
        return super().run(transport=transport, mount_path=mount_path)

    def _build_state_machine(self) -> None:
        """Startup-only bootstrap for the state machine (single, session-aware instance).

        Builds the machine exactly once after all user registrations via the DSL.
        Validation is performed by the builder during `build()`. The resulting
        StateMachine is session-aware through the `ctx` parameter on its API.
        """
        if self._state_machine is not None:
            return

        logger.debug("State machine bootstrap: begin building and validating from DSL")

        internal = self._state_definition._to_internal_builder()  # pyright: ignore[reportPrivateUsage]
        self._state_machine = internal.build()
        logger.debug("State machine bootstrap: build complete and ready")

    def _init_state_aware_managers(self) -> None:
        """Instantiate state-aware managers once the state machine exists."""
        if self._state_machine is None:
            raise RuntimeError("State machine must be built before initializing stateful managers")
        
        if self._stateful_tools is None:
            logger.debug("State machine wiring: initializing StateAwareToolManager")
            self._stateful_tools = StateAwareToolManager(
                state_machine=self._state_machine,
                tool_manager=self._tool_manager,
            )

        if self._stateful_resources is None:
            logger.debug("State machine wiring: initializing StateAwareResourceManager")
            self._stateful_resources = StateAwareResourceManager(
                state_machine=self._state_machine,
                resource_manager=self._resource_manager,
            )

        if self._stateful_prompts is None:
            logger.debug("State machine wiring: initializing StateAwarePromptManager")
            self._stateful_prompts = StateAwarePromptManager(
                state_machine=self._state_machine,
                prompt_manager=self._prompt_manager,
            )

    # ----------------------------
    # Overridden FastMCP methods (delegating to state-aware managers)
    # ----------------------------

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        """Override FastMCP.

        Execute via the state-aware ToolManager. Access is constrained by the
        session-scoped state machine to tools allowed in the *current state* of the *current session*. 
        """
        assert self._stateful_tools is not None, "Stateful managers not initialized; call run() first"
        return await self._stateful_tools.call_tool(name, arguments, self.get_context())


    async def read_resource(self, uri: AnyUrl | str) -> Iterable[ReadResourceContents]:
        """Override FastMCP.

        Read via the state-aware ResourceManager. Access is constrained by the
        session-scoped state machine to resources allowed in the *current state* of the *current session*.
        """
        assert self._stateful_resources is not None, "Stateful managers not initialized; call run() first"
        return await self._stateful_resources.read_resource(uri, self.get_context())


    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> GetPromptResult:
        """Override FastMCP.

        Resolve via the state-aware PromptManager. Access is constrained by the
        session-scoped state machine to prompts allowed in the *current state* of the *current session*.
        """
        assert self._stateful_prompts is not None, "Stateful managers not initialized; call run() first"
        return await self._stateful_prompts.get_prompt(name, arguments or {}, self.get_context())


    async def list_tools(self) -> list[MCPTool]:
        """Override FastMCP.

        List via the state-aware ToolManager. Returns only tools permitted by the
        session-scoped state machine for the *current state* of the *current session*.
        """
        assert self._stateful_tools is not None, "Stateful managers not initialized; call run() first"
        tools = self._stateful_tools.list_tools(self.get_context())
        return [
            MCPTool(
                name=tool.name,
                title=tool.title,
                description=tool.description,
                inputSchema=tool.parameters,
                outputSchema=tool.output_schema,
                annotations=tool.annotations,
            )
            for tool in tools
        ]


    async def list_resources(self) -> list[MCPResource]:
        """Override FastMCP.

        List via the state-aware ResourceManager. Returns only resources permitted by
        the session-scoped state machine for the *current state* of the *current session*.
        """
        assert self._stateful_resources is not None, "Stateful managers not initialized; call run() first"
        resources = await self._stateful_resources.list_resources(self.get_context())
        return [
            MCPResource(
                uri=resource.uri,
                name=resource.name or "",
                title=resource.title,
                description=resource.description,
                mimeType=resource.mime_type,
            )
            for resource in resources
        ]


    async def list_prompts(self) -> list[MCPPrompt]:
        """Override FastMCP.

        List via the state-aware PromptManager. Returns only prompts permitted by
        the session-scoped state machine for the *current state* of the *current session*.
        """
        assert self._stateful_prompts is not None, "Stateful managers not initialized; call run() first"
        prompts = self._stateful_prompts.list_prompts(self.get_context())
        return [
            MCPPrompt(
                name=prompt.name,
                title=prompt.title,
                description=prompt.description,
                arguments=[
                    MCPPromptArgument(name=a.name, description=a.description, required=a.required)
                    for a in (prompt.arguments or [])
                ],
            )
            for prompt in prompts
        ]

    # Special case for ResourceTemplates => violated DEA constraint (finite Σ)
    async def list_resource_templates(self) -> list[MCPResourceTemplate]:
        """
        Override FastMCP.

        Resource templates are not exposed by the DFA-based server. All resources
        (including dynamic templates) are normalized into concrete resources and
        surfaced via `list_resources` only.

        Clients MUST use `list_resources` to discover resources. This handler
        always fails to make the unsupported feature explicit.
        """
        raise RuntimeError(
            "This server does not expose resource templates. "
            "All resources (including dynamic templates) are flattened and "
            "made available via `list_resources` only."
        )
