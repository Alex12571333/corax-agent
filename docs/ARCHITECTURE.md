# Architecture

Corax Agent is built as a thin **lifecycle + extension** layer. The guiding
rule: *scaffold first, real capabilities later — without restructuring.*

## Layers

```
main.py                CLI: argparse -> asyncio.run
   │
CoraxApp (app.py)      boot / run_menu / shutdown
   │
   ├── config.py       AgentConfig dataclasses, load/save/validate
   ├── paths.py        resolve + create dirs, blocked-path guard
   ├── logging_setup   console + file logging
   ├── settings.py     get/set/toggle/activate (the only config mutator)
   ├── menu.py + ui/   terminal settings menu (injectable I/O)
   │
   └── CoraxRuntime (runtime.py)
          ├── ProviderRegistry          (planner lives here)
          ├── MemoryRegistry
          ├── ConnectorRegistry
          └── CapabilityRegistryAdapter
                  ▲
                  └── populated from config using stubs/ only
```

## Boot sequence

`CoraxApp.boot()`:

1. **load config** (create default + flag first-run if missing)
2. **ensure paths** (`workspace/`, `data/`, `logs/`)
3. **setup logging** (level from config, file under `logs/`)
4. **init runtime** (`CoraxRuntime(config)`)
5. **start runtime** (populate registries from config, stubs only)

`run_menu()` then shows the settings menu. `shutdown()` saves the config if
it changed and stops the runtime.

## Runtime & registries

`CoraxRuntime` owns four registries and fills them on `start()` from the
active config:

| Config section | Registry                   | Stub registered          |
|----------------|----------------------------|--------------------------|
| `planner`      | `ProviderRegistry`         | `PlannerStub`            |
| `memory`       | `MemoryRegistry`           | `MemoryStub`             |
| `connectors`   | `ConnectorRegistry`        | `ConnectorStub`          |
| `capabilities` | `CapabilityRegistryAdapter`| `CapabilityStub`         |

Mapping from a config id to a concrete object lives in the small factory
tables at the top of `runtime.py` (`_PLANNER_FACTORIES`, …). Adding a real
implementation = adding an entry there. The start/stop/status/reload lifecycle
never changes.

`RuntimeStatus` is a serialisable snapshot (`to_dict()` / `render()`), exposed
both via `await runtime.status()` and the synchronous `runtime.snapshot()`
(used by the blocking menu).

## Why stubs

Stubs give every role a concrete, well-formed member so the runtime, menu and
(future) execution pipeline work end-to-end with zero external dependencies.
Replacing a stub means registering a different object under the same role —
call sites are unaffected.

## Security guard

`paths.is_blocked_path()` enforces `security.blocked_paths` (notably
`../corax-core` and `../corax-sdk`). No code in this stage writes files or runs
shells; the guard is the seam future file/shell capabilities must call.
