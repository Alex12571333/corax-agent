# Architecture

Corax Agent is built as a thin **lifecycle + extension** layer. The guiding
rule: *scaffold first, real capabilities later — without restructuring.*

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
          ├── ProviderRegistry          (planner lives here)
          ├── MemoryRegistry
          ├── ConnectorRegistry
          └── CapabilityRegistryAdapter
                  ▲
                  ├── built-ins:  planner/ · connectors/ · memory/ · capabilities/
                  └── loader/:    SDK capability packages (filesystem, editor, shell)

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

`CoraxRuntime` owns four registries and fills them on `start()` from the
active config:

| Config section | Registry                    | Built-in registered      |
|----------------|-----------------------------|--------------------------|
| `planner`      | `ProviderRegistry`          | `StubPlanner`            |
| `memory`       | `MemoryRegistry`            | `NullMemory`             |
| `connectors`   | `ConnectorRegistry`         | `TerminalConnector`      |
| `capabilities` | `CapabilityRegistryAdapter` | `EchoCapability` + packages |

Mapping from a config id to a concrete class lives in the small factory
tables at the top of `runtime.py` (`_PLANNER_FACTORIES`, …). Adding a real
implementation = adding an entry there. The start/stop/status/reload lifecycle
never changes.

Capability **packages** (filesystem, editor, shell) are not in the factory
table; they are loaded by `CapabilityLoader`, which reads each package's root
`capability.json`, validates it against the core version and instantiates it
through `agent-sdk`. That dependency is imported lazily, so the scaffold runs
on a pure-stdlib install — packages are simply skipped with a warning.

## Execution kernel (agent-core)

`CoreEngine` (`corax/loader/core.py`) is the second lazy seam — the mirror of
the capability loader, but for the **execution kernel** rather than for tools.
It imports `agent-core` lazily and, on demand, assembles a fully-wired kernel:
`CapabilityRegistry`, `Router`, `DefaultPolicyEngine`, session/state/task stores,
an `EventBus`, a `TraceManager` and the async `Executor` — with limits taken
from the config's `limits` section.

The runtime owns one `CoreEngine` (`runtime.core`). It does not run a perpetual
worker loop (the CLI has no persistent event loop); instead the kernel is built,
used and torn down inside the caller's loop:

```python
task = await runtime.execute("filesystem", input={"operation": "read", "path": "x"})
```

`runtime.execute()` opens `core.session(self.capabilities)`, which registers only
the **real** `agent_core.Capability` instances (the SDK-loaded ones; built-in
placeholders like `echo` are skipped), starts the executor worker, runs one task
through the full route → policy → execute → settle pipeline, and shuts down. When
`agent-core` is absent, `runtime.core.available` is `False`, `RuntimeStatus`
reports the kernel as unavailable, and `execute()` raises a clear `RuntimeError`.

`RuntimeStatus` is a serialisable snapshot (`to_dict()` / `render()`), exposed
both via `await runtime.status()` and the synchronous `runtime.snapshot()`
(used by the blocking menu).

## Why built-ins

Each role ships one concrete, well-formed member so the runtime, menu and
(future) execution pipeline work end-to-end with zero external dependencies.
Replacing a built-in means registering a different class under the same role —
call sites are unaffected.

## Security guard

`paths.is_blocked_path()` enforces `security.blocked_paths` (notably
`../corax-core` and `../corax-sdk`). No code in this stage writes files or runs
shells directly; the guard is the seam future file/shell capabilities call.
