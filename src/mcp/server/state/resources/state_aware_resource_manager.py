from __future__ import annotations

from typing import Iterable, Optional

from pydantic import AnyUrl

from mcp.server.fastmcp.exceptions import ResourceError
from mcp.server.fastmcp.resources import Resource, ResourceManager, FunctionResource
from mcp.server.fastmcp.utilities.logging import get_logger
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.state.helper.extract_session_id import extract_session_id
from mcp.server.state.machine.state_machine import StateMachine, SessionScope
from mcp.server.state.types import FastMCPContext

logger = get_logger(__name__)


def _sid(ctx: Optional[FastMCPContext]) -> Optional[str]:
    """Best-effort: extract session id from ctx; None on failure/missing."""
    if ctx is None:
        return None
    try:
        return extract_session_id(ctx)
    except Exception:
        return None


class StateAwareResourceManager:
    """State-aware **facade** over ``ResourceManager``.

    Wraps a ``StateMachine`` and delegates to the native manager while constraining
    discovery and reads by the machine's *current state*.

    Facade model (simplified):
    - Discovery via ``state_machine.available_symbols('resource')`` (URIs).
    - `state_machine.step(...)` emits SUCCESS/ERROR around the read. Edge effects are best-effort.

    Session model:
    - Session is ambient (ContextVar). We bind per call via ``SessionScope(_sid(ctx))``.
    """

    def __init__(
        self,
        state_machine: StateMachine,
        resource_manager: ResourceManager,
    ):
        self._resource_manager = resource_manager
        self._state_machine = state_machine

    async def list_resources(self, ctx: Optional[FastMCPContext] = None) -> list[Resource]:
        """
        Return concrete resources allowed in the current state (names via ``available_symbols('resource')``).

        The DFA operates only on explicit resource URIs. We therefore resolve the allowed
        URIs against the server's registered static resources first, then use resource
        templates as a fallback.

        Templates are flattened into plain Resource metadata so the DFA never sees dynamic
        patterns. Missing registrations are logged as warnings.
        """
        with SessionScope(_sid(ctx)):
            allowed_uris = self._state_machine.available_symbols("resource")  # Set[str]

            # Retrieve static resources first so they take precedence over templates.
            static_resources = {
                str(res.uri): res for res in self._resource_manager.list_resources()
            }

            # Retrieve all registered templates for template fallback.
            templates = self._resource_manager.list_templates()

            out: list[Resource] = []

            for uri in allowed_uris:
                # Prefer a concrete resource if one is registered for this URI.
                static_res = static_resources.get(uri)
                if static_res is not None:
                    out.append(static_res)
                    continue

                # Template fallback: find the first template whose URI pattern matches.
                templated_res: Resource | None = None
                for tmpl in templates:
                    if tmpl.matches(uri) is not None:
                        templated_res = FunctionResource(
                            uri=AnyUrl(uri), 
                            name=tmpl.name,
                            title=tmpl.title,
                            description=tmpl.description,
                            mime_type=tmpl.mime_type,
                            icons=tmpl.icons,
                            annotations=tmpl.annotations,
                            fn=tmpl.fn
                        )
                        break

                if templated_res is not None:
                    out.append(templated_res)
                else:
                    logger.warning(
                        "Resource '%s' expected in state '%s' but not registered "
                        "as static resource or template.",
                        uri,
                        self._state_machine.current_state(),
                    )

            return out


    async def read_resource(
        self,
        uri: str | AnyUrl,
        ctx: FastMCPContext,
    ) -> Iterable[ReadResourceContents]:
        """Read the resource in the **current state** with SUCCESS/ERROR step semantics."""
        with SessionScope(_sid(ctx)):
            allowed = self._state_machine.available_symbols("resource")
            uri_str = str(uri)
            if uri_str not in allowed:
                raise ResourceError(
                    f"Resource '{uri}' is not allowed in state '{self._state_machine.current_state()}'. "
                    f"Use list_resources() to inspect availability."
                )

            resource = await self._resource_manager.get_resource(uri_str, ctx)
            if not resource:
                raise ResourceError(f"Unknown resource: {uri}")

            # State step scope (effects can use ctx; scope does not rebind session)
            async with self._state_machine.step(kind="resource", ident=uri_str, ctx=ctx):
                content = await resource.read()
                return [ReadResourceContents(content=content, mime_type=resource.mime_type)]
