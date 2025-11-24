# pyright: reportPrivateUsage=false, reportUnusedFunction=false, reportUnusedImport=false, reportUnusedVariable=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeArgument=false, reportUnknownParameterType=false, reportAssignmentType=false

import asyncio

import pytest
from pytest import LogCaptureFixture

from mcp.server.fastmcp.server import Context
from mcp.server.state.server import StatefulMCP


@pytest.mark.anyio
async def test_context_injected_on_effect(caplog: LogCaptureFixture):
    """Ensure that when a Context resolver is available, the Context is injected into the effect."""
    caplog.set_level("DEBUG")

    app = StatefulMCP(name="inject_ctx_prompt_effect")

    called = {}

    async def ctx_effect(ctx: Context) -> str:
        called["ctx"] = ctx
        return "ok"

    @app.tool()
    def t_test() -> str:
        return "ok"

    # sanity: tool is registered
    assert app._tool_manager.get_tool("t_test") is not None

    # minimal machine: s0 -> s1 (callback expects Context)
    (
        app.statebuilder
            .define_state("s0", is_initial=True)
            .on_tool("t_test").on_success("s1", terminal=True, effect=ctx_effect)
            .build_edge()
    )

    app._build_state_machine()
    app._init_state_aware_managers()

    sm = app._state_machine
    assert sm is not None

    # this does trigger the tool (stateful manager)
    await app.call_tool("t_test", {})

    # let the async effect run
    for _ in range(10):
        if "ctx" in called:
            break
        await asyncio.sleep(0.01)

    assert "ctx" in called, "Callback should have been called"
    assert called["ctx"] is not None, "Context should have been injected"
    assert any("Injecting context parameter for target" in rec.message for rec in caplog.records)
