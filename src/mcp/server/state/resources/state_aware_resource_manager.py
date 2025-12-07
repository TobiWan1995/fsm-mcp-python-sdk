from __future__ import annotations

from typing import Iterable, Optional

from pydantic import AnyUrl

from mcp.server.fastmcp.exceptions import ResourceError
from mcp.server.fastmcp.resources import Resource, ResourceManager, FunctionResource
from mcp.server.fastmcp.utilities.logging import get_logger
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.state.helper.extract_session_id import extract_session_id
from mcp.server.state.machine.state_machine import StateMachine, SessionScope, InputSymbol
from mcp.server.state.types import FastMCPContext, ResultType

logger = get_logger(__name__)

class StateAwareResourceManager:
    """State-aware facade over ``ResourceManager``.

    Wraps a ``StateMachine`` and delegates to the native manager while constraining
    discovery and reads by the machine's current state.

    Facade model (simplified)
    - Discovery via ``state_machine.available_symbols("resource")``.
    - ``state_machine.step(...)`` emits SUCCESS or ERROR around the read.

    Session model:  ambient via ``SessionScope(extract_session_id(ctx))``.
    """

    def __init__(
        self,
        state_machine: StateMachine,
        resource_manager: ResourceManager,
    ):
        self._resource_manager = resource_manager
        self._state_machine = state_machine

    def _get_and_validate_resource(
        self,
        uri: str,
        symbols: Iterable[InputSymbol],
    ) -> Resource:
        """
        Resolve and validate a resource binding in the current state.

        This enforces that
        - at least one symbol with ``ident == uri`` exists in the current state
        - the binding covers the full ResultType space (one symbol per variant)
        - the resource is registered in the underlying ResourceManager
          either as a static resource or via a matching template
        """
        state_name = self._state_machine.current_state()

        # 1) Ensure the DFA actually exposes this URI in the current state.
        matching = [s for s in symbols if s.ident == uri]
        if not matching:
            raise ResourceError(
                f"Resource '{uri}' is not allowed in state '{state_name}'. "
                "Use list_resources() to inspect availability."
            )

        # 2) Check completeness of the result space.
        observed_results = {s.result for s in matching}
        expected_results = set(ResultType)

        if observed_results != expected_results:
            raise ResourceError(
                "Inconsistent state machine configuration. "
                f"Binding 'resource/{uri}' in state '{state_name}' "
                "does not cover the complete result space."
            )

        # 3) Resolve the URI against static resources and templates.
        #    The DFA operates only on explicit resource URIs. We therefore resolve
        #    the allowed URIs against the server's registered static resources first,
        #    then use resource templates as a fallback. Templates are flattened into
        #    plain Resource metadata so the DFA never sees dynamic patterns.
        static_resources = {
            str(res.uri): res for res in self._resource_manager.list_resources()
        }
        templates = self._resource_manager.list_templates()

        static_res = static_resources.get(uri)
        if static_res is not None:
            return static_res

        # Template fallback: find the first template whose URI pattern matches.
        for tmpl in templates:
            if tmpl.matches(uri) is not None:
                return FunctionResource(
                    uri=AnyUrl(uri),
                    name=tmpl.name,
                    title=tmpl.title,
                    description=tmpl.description,
                    mime_type=tmpl.mime_type,
                    icons=tmpl.icons,
                    annotations=tmpl.annotations,
                    fn=tmpl.fn,
                )

        raise ResourceError(
            f"Resource '{uri}' is referenced in state '{state_name}' "
            "but is not registered as static resource or template."
        )

    async def list_resources(
        self,
        ctx: Optional[FastMCPContext] = None,
    ) -> list[Resource]:
        """
        Return concrete resources allowed in the current state.

        The DFA operates only on explicit resource URIs. Allowed URIs are resolved
        against the server's registered static resources first, then against
        resource templates which are flattened into plain Resource metadata.
        """
        with SessionScope(extract_session_id(ctx)):
            symbols = self._state_machine.get_symbols("resource")
            allowed_uris = {s.ident for s in symbols}

            out: list[Resource] = []
            for uri in allowed_uris:
                res = self._get_and_validate_resource(uri, symbols)
                out.append(res)

            return out

    async def read_resource(
        self,
        uri: str | AnyUrl,
        ctx: FastMCPContext,
    ) -> Iterable[ReadResourceContents]:
        """Read a resource in the current state with SUCCESS/ERROR step semantics."""
        with SessionScope(extract_session_id(ctx)):
            symbols = self._state_machine.get_symbols("resource")
            uri_str = str(uri)

            resource = self._get_and_validate_resource(uri_str, symbols)

            # State step scope (effects can use ctx; scope does not rebind session)
            async with self._state_machine.step(kind="resource", ident=uri_str, ctx=ctx):
                content = await resource.read()
                return [ReadResourceContents(content=content, mime_type=resource.mime_type)]
