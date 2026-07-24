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
confirmation protocol. Concrete filesystem, shell, network, and connector
packages still enforce their own technical boundaries. `BLOCKED` and
administrator deny rules are not bypassed by full mode.
