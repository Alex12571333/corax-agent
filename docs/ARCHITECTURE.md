# Architecture

Corax Agent is a **lifecycle + typed extension** layer. A common package format
does not imply a common execution role.

## Layers

```
main.py                CLI: argparse -> asyncio.run
   │
CoraxApp (corax/app.py)        boot / run_menu / shutdown
   │
   ├── config.py       AgentConfig dataclasses, load/save/validate
   ├── yaml_lite.py    minimal YAML reader/writer (used when PyYAML is absent)
   ├── paths.py        resolve + create dirs, blocked-path guard
   ├── logging.py      console + file logging
   ├── settings.py     get/set/toggle/activate (the only config mutator)
   ├── health.py       uniform Health payload for built-ins
   ├── ui/             terminal menu, screens, banner (injectable I/O)
   │
   └── CoraxRuntime (corax/runtime.py)
          └── ExtensionCatalog
                  ├── ToolRegistry
                  ├── ChannelRegistry
                  ├── ModelRegistry
                  ├── MemoryRegistry
                  ├── PolicyRegistry
                  └── ServiceRegistry
                  ▲
                  ├── built-ins: planner/ · connectors/ · memory/ · capabilities/
                  └── loader/: typed SDK extension packages

   CoreEngine (corax/loader/core.py)   ← agent-core execution kernel (lazy)
          Executor · Router · Policy · Session/State/Task stores · EventBus · Tracer
```

## Package layout

Code is grouped **by role**, the same way real implementations will be named:

| Folder               | Role           | Built-in shipped today                    |
|----------------------|----------------|-------------------------------------------|
| `corax/planner/`     | planner        | `StubPlanner` (`stub.py`)                  |
| `corax/connectors/`  | I/O surfaces   | `TerminalConnector` (`terminal.py`)        |
| `corax/memory/`      | memory backend | `NullMemory` (`none.py`)                   |
| `corax/capabilities/`| tools          | `EchoCapability` (`echo.py`)               |
| `corax/registry/`    | extension seams| `Registry` + one subclass per role         |
| `corax/loader/`      | external seams | `CapabilityLoader` (agent-sdk) · `CoreEngine` (agent-core) |
| `corax/ui/`          | terminal UI    | `Menu`, `Terminal`, `screens`, `banner`    |

## Boot sequence

`CoraxApp.boot()`:

1. **load config** (create default + flag first-run if missing)
2. **ensure paths** (`workspace/`, `data/`, `logs/`)
3. **setup logging** (level from config, file under `logs/`)
4. **init runtime** (`CoraxRuntime(config)`)
5. **start runtime** (populate registries from config)

`run_menu()` then shows the settings menu. `shutdown()` saves the config if
it changed and stops the runtime.

## Runtime & registries

`CoraxRuntime` owns an `ExtensionCatalog` and fills its role registries from
`extensions.active`:

| Kind | Registry | Built-in |
| --- | --- | --- |
| `tool` | `ToolRegistry` | `EchoCapability` |
| `channel_connector` | `ChannelRegistry` | `TerminalConnector` |
| `model_provider` | `ModelRegistry` | `StubPlanner` |
| `memory_provider` | `MemoryRegistry` | `NullMemory` |
| `policy_provider` | `PolicyRegistry` | external `security.policy` |
| `runtime_service` | `ServiceRegistry` | external gateway |

Mapping from a config id to a concrete class lives in the small factory
tables at the top of `runtime.py` (`_PLANNER_FACTORIES`, …). Adding a real
implementation = adding an entry there. The start/stop/status/reload lifecycle
never changes.

External packages are loaded by `ExtensionLoader`, which reads each root
`extension.json`, validates it against Core/SDK versions, instantiates it, and
verifies the declared kind against the Python contract. That dependency is
imported lazily.

## Execution kernel (agent-core)

`CoreEngine` (`corax/loader/core.py`) is the second lazy seam — the mirror of
the capability loader, but for the **execution kernel** rather than for tools.
It imports `agent-core` lazily and, on demand, assembles a fully-wired kernel:
`CapabilityRegistry`, `Router`, the bound `policy_provider` (or conservative
`DefaultPolicyEngine` fallback), session/state/task stores, an `EventBus`, a
`TraceManager` and the async `Executor` — with limits taken from the config's
`limits` section.

The runtime owns one `CoreEngine` (`runtime.core`). It does not run a perpetual
worker loop (the CLI has no persistent event loop); instead the kernel is built,
used and torn down inside the caller's loop:

```python
task = await runtime.execute("filesystem", input={"operation": "read", "path": "x"})
```

`runtime.execute()` opens `core.session(self.tools, policy=self.active_policy())`, registers only
`agent_core.ToolCapability` instances, starts the executor worker, runs one task
through the full route → policy → execute → settle pipeline, and shuts down. When
`agent-core` is absent, `runtime.core.available` is `False`, `RuntimeStatus`
reports the kernel as unavailable, and `execute()` raises a clear `RuntimeError`.

Models, channels, memory and services never enter that tool registry. They are
called by `runtime.invoke_extension()` (or model streaming) through their own
contracts and are selected through config bindings such as `primary_model` and
`memory`.

`memory.loop` is a runtime service bound to the selected `memory_provider`.
Conversation channels call it before and after each turn. Recall is bounded and
injected as untrusted context; retention is conservative and excludes secrets
and assistant-authored claims.

`state.file` implements the core `StorageProvider` port. The console uses it
for bounded restart-safe checkpoints; it is runtime-only and never model-callable.

`context.manager` runs inside the model-provider host path, so console,
Telegram, streaming and non-streaming requests share one deterministic context
budget without adding another model call.

`prompts.runtime` owns layered Markdown assembly for every user-facing channel.
It freezes file/profile/skill layers for a turn, keeps effective hidden replay
in RAM, and appends runtime, recall, selected schemas, tool-loop messages, and
new user turns. Checkpoints continue to contain only raw user/assistant history;
a restart is cache-cold but does not persist schemas or recalled private data.
Prompt traces contain only layer/tool IDs, sizes, and hashes—not descriptors,
schemas, compiled prompts, profile text, or recalled memory.

`mcp.manager` uses the official MCP client SDK and registers discovered remote
tools only after connection. Those proxies still execute through Agent Core and
cannot bypass schema validation or the active policy. MCP
`notifications/tools/list_changed` marks a server catalog dirty; the host
refreshes and reconciles add/change/remove updates before the next model call.

## Tool discovery and per-turn schemas

The full executable registry stays host-side. `ToolCatalog` contains compact
routing records (intent examples, anti-examples, operations, channel and
security/cost hints), while `SchemaStore` is the only owner of full input
schemas. Stable schema and routing hashes let reloads re-embed only changed
cards; removed tools disappear from the catalog and the live Agent Core
registry.
At each user turn the host filters blocked/channel-incompatible tools, embeds
the request with `Nemotron-3-Embed-1B` on the configured `.10:8080` endpoint,
and creates a bounded `TurnToolSet`. The set's schemas enter the turn envelope,
while the provider's top-level tool list is the fixed `tool_search` +
`tool_call` pair. Console, TUI, and Telegram use the same runtime method.

Routing never calls a generation LLM. If embeddings are unavailable, a small
deterministic lexical fallback may select matching tools, but failure never
reveals the full catalog. The host-managed `tool.search` meta-tool can add
matches and their bounded schemas to the current turn. `tool.call` unwraps the
chosen id only in the host. Agent Core remains the execution boundary and
rejects calls to tools outside the active turn before policy and execution.

`skills.runtime` implements progressive disclosure for portable Agent Skills.
It reads metadata from bounded, trusted roots and injects only selected
instructions before the shared context-compaction boundary.

`hooks.runtime` dispatches model/tool lifecycle events to explicitly approved
subprocess commands. Hook wrappers preserve each tool's original schema, scopes,
risk and side effects; the Agent Core policy boundary remains unchanged.

`subagents.orchestrator` owns bounded parallel leaf delegation and registers
`subagents.delegate` as a confirm-gated core tool. Child calls use the selected
model provider in isolated sessions and receive no tool registry.

`sandbox.executor` is a host-only backend injected into the shell tool. The
shell keeps validation/redaction and Agent Core policy; process execution is
fail-closed inside Seatbelt or Docker.

`model.router` is a `model_provider` composed over other loaded providers. It
routes by modalities and context size, supports model overrides, and performs
fallback only for retryable failures.

`observability.jsonl` is an `observability` provider. The host adapts it to the
kernel trace-writer port and emits model lifecycle records directly. It never
enters tool routing, and telemetry failures are isolated from execution.

`RuntimeStatus` is a serialisable snapshot (`to_dict()` / `render()`), exposed
both via `await runtime.status()` and the synchronous `runtime.snapshot()`
(used by the blocking menu).

## Why built-ins

Each role ships one concrete, well-formed member so the runtime, menu and
(future) execution pipeline work end-to-end with zero external dependencies.
Replacing a built-in means registering a different class under the same role —
call sites are unaffected.

## Security control plane

`security.policy` owns ask/auto/full authorization decisions and operator
commands. Agent Core owns the generic policy checkpoint and one-time
confirmation protocol, including digest-bound turn/session leases. Concrete
filesystem, shell, network, and connector packages still enforce their own
technical boundaries. `BLOCKED` and administrator deny rules are not bypassed
by full mode. Telegram document delivery remains host-controlled; the model
catalogue does not expose a direct connector pseudo-tool until it can be
represented as a typed kernel ToolCapability.
