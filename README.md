# FSM MCP Python SDK

> **Note:** This is a specialized extension of the official Model Context Protocol (MCP) Python SDK designed for formal process modeling and state-based access control. For standard implementations without state machine logic, please refer to the [official repository](https://www.google.com/search?q=LINK_TO_REPO_2).

## About this Project

This SDK was developed as part of a Master's thesis at the Technische Hochschule Mittelhessen (December 2025). It extends the MCP protocol with server-side state management based on Deterministic Finite Automata (DFA).

The goal is to provide **State-Aware Orchestration**. While standard MCP servers typically expose a flat list of tools and resources, this SDK enables the definition of directed graphs where the availability of tools, resources, and prompts is strictly controlled by the current state of the session.

### Scientific Background: Orchestration vs. Sampling

The Model Context Protocol provides powerful mechanisms like **Server Sampling** to realize multi-turn workflows. Through sampling, a server can instruct the client to execute a language model with specific configurations, allowing the model to utilize server tools within an active request context.

While sampling enables agentic behavior and encapsulated loops within tools, the FSM architecture addresses different architectural challenges:

1.  **Structural Control vs. Agentic Autonomy:** Sampling relies on the model's agency to select tools in the correct order. The FSM approach enforces valid paths at the protocol level. A tool that is not valid in the current state is technically unavailable to the client, preventing out-of-order execution by design.
2.  **Transparency:** In sampling, the process logic is often encapsulated within the implementation of a single tool. The FSM approach externalizes the process model, making the allowed states and transitions explicit.
3.  **Orchestration:** This SDK is designed to compose atomic tools into ordered workflows that can be formally validated for reachability and consistency.

**The FSM approach is intended to complement, not replace, sampling.**
Sampling is an excellent mechanism for solving complex tasks *within* a specific state (e.g., "Drafting a Report"), while the FSM defines the high-level lifecycle and guardrails of the interaction (e.g., "Drafting" $\rightarrow$ "Review" $\rightarrow$ "Approval").

## Architecture and Core Concepts

The server inherits directly from `FastMCP` and maintains full protocol compatibility. It modifies the discovery and execution handlers to enforce state constraints.

### 1\. State-Aware Managers

The SDK aggregates the native FastMCP managers into **State-Aware Managers**. These act as proxies, filtering access to **Tools**, **Prompts**, and **Resources** based on the current state configuration.

### 2\. Separation of Control Flow and Domain Context

A key architectural distinction is made between the process and the data:

  * **The Automaton (FSM):** Controls the abstract control flow (Which actions are allowed? Where does success or failure lead?).
  * **The Lifespan Context:** Serves as domain-specific memory for concrete data (e.g., user inputs, counter variables, cart contents).
    Both systems work synchronously: The FSM dictates availability, while the Context holds the data required for tool logic.

### 3\. Degradation of Resource Templates

Since a Deterministic Finite Automaton (DFA) requires a finite set of input symbols ($\Sigma$), dynamic **Resource Templates** (which match infinite URIs) cannot be exposed as variable symbols within the graph.

  * **Behavior:** The method `resources/templates/list` is disabled and raises an error to make this constraint explicit.
  * **Solution:** Templates are concretized into **static resources** within the automaton. A state references a specific, concrete URI (e.g., `greeting://alice`). The validator accepts this URI if a matching template is registered in the background to handle the request.

### 4\. Builder & Validation

The state machine is defined declaratively via a **Fluent Interface**. Before the server starts, the system validates the graph for structural integrity, ensuring that:

  * An initial state exists.
  * Terminal states are reachable from the start.
  * The result space (Success/Error) is fully covered for all bound artifacts.

## Installation

This package is currently available via direct repository installation:

```bash
pip install "mcp @ git+https://github.com/TobiWan1995/fsm-mcp-python-sdk.git@v0.1.0"
```

## Usage

The following example ("Crossroads") illustrates the definition of a control flow using the `StateBuilder`. It demonstrates how tools are bound to states and how transitions (`on_success`, `on_error`) are defined.

The `effect` parameter is utilized here to trigger side effects—such as notifying the client that the tool list has changed—whenever a transition occurs.

> **Note:** This snippet focuses on the FSM definition. The full implementation, including the `LifespanContext` and the actual tool logic, can be found in the [fsm-mcp-examples](https://www.google.com/search?q=LINK_TO_EXAMPLES_REPO) repository.

```python
from mcp.server.state import StatefulMCP
from mcp.server.fastmcp import Context
from crossroads.lifespan import app_lifespan
from crossroads.tools import register_tools

app = StatefulMCP("Crossroads", lifespan=app_lifespan)

register_tools(app)

async def tool_list_changed(ctx: Context):
    """Notifies the client that the list of available tools has changed."""
    await ctx.session.send_tool_list_changed()

# ------------------------------------
# Definition of State Machine
# ------------------------------------

graph = (
    app.statebuilder
    # Entry: Start state with retry behavior on error
    .define_state("C_entry", is_initial=True)
        .on_tool("t_open_door")
            .on_success("C_crossroad", effect=tool_list_changed).build_edge()
            # on_error: implicit self-loop on C_entry (user must try again)
        .build_state()

    # Crossroad: A branching point in the process
    .define_state("C_crossroad")
        .on_tool("t_press_button")
            .on_success("C_crossroad").build_edge()
        .on_tool("t_choose_left_path")
            .on_success("C_doorL", effect=tool_list_changed).build_edge()
        .on_tool("t_choose_right_path")
            .on_success("C_doorR", effect=tool_list_changed).build_edge()
        .build_state()

    # Left Path
    .define_state("C_doorL")
        .on_tool("t_open_door_with_key")
            # Terminal state: Process successfully finished
            .on_success("C_doorL", terminal=True, effect=tool_list_changed)
            .on_error("C_rollback_left", effect=tool_list_changed).build_edge()
        .on_tool("t_pick_up_key")
            .on_success("C_doorL").build_edge()
        .build_state()

    # Right Path (Symmetrical)
    .define_state("C_doorR")
        .on_tool("t_open_door_with_key")
            .on_success("C_doorR", terminal=True, effect=tool_list_changed)
            .on_error("C_rollback_right", effect=tool_list_changed).build_edge()
        .on_tool("t_pick_up_key")
            .on_success("C_doorR").build_edge()
        .build_state()

    # Rollback Logic (e.g., if the wrong key was used)
    .define_state("C_rollback_left")
        .on_tool("t_go_back")
            .on_success("C_crossroad", effect=tool_list_changed).build_edge()
        .on_tool("t_open_door_with_key")
            .on_success("C_doorL", terminal=True, effect=tool_list_changed)
            .on_error("C_rollback_left").build_edge()
        .build_state()

    .define_state("C_rollback_right")
        .on_tool("t_go_back")
            .on_success("C_crossroad", effect=tool_list_changed).build_edge()
        .on_tool("t_open_door_with_key")
            .on_success("C_doorR", terminal=True, effect=tool_list_changed)
            .on_error("C_rollback_right").build_edge()
        .build_state()
)

# ------------------------------------
# Run the Server (SSE Default)
# ------------------------------------
if __name__ == "__main__":
    app.run(transport="sse")
```

## Further Resources

  * **Examples (Server-Side):** For complex scenarios demonstrating the interaction between Lifespan Context and the State Machine, please visit the [fsm-mcp-examples](https://www.google.com/search?q=LINK_TO_EXAMPLES_REPO) repository.
  * **Client Reference:** An example client implementation that handles state change notifications can be found in the [fsm-mcp-client](https://www.google.com/search?q=LINK_TO_CLIENT_REPO) repository.
  * **Standard SDK:** For standard use cases without state machine logic, please use the official [mcp-python-sdk](https://www.google.com/search?q=LINK_TO_STANDARD_REPO).