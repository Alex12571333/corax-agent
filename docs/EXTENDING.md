# Extending Corax

Every future module plugs into an existing seam. You should never need to
restructure the package to add one.

## The extension points

1. **config** — declare the provider/capability in `corax.yaml`.
2. **role folder** — drop your class next to the built-in (`corax/planner/`,
   `corax/connectors/`, `corax/memory/`, `corax/capabilities/`).
3. **factory table** — map the config id to your class in `corax/runtime.py`.
4. **(optionally) settings** — extra fields are read via `get_setting`.

## Recipe: add a real planner

Say you want an `OpenAIPlanner`.

### 1. Implement it

Create `corax/planner/openai.py` next to `stub.py`, with the same shape the
runtime expects of a planner (an `async plan(...)`, `async health()`, an `id`):

```python
from ..health import Health


class OpenAIPlanner:
    id = "planner.openai"
    kind = "planner"

    def __init__(self, **opts): ...
    async def plan(self, goal, *, correlation_id=None): ...
    async def health(self) -> Health: ...
```

> Keep secrets out of the repo — read them from env at construction.

### 2. Register it in the factory table

In `corax/runtime.py`:

```python
from .planner.openai import OpenAIPlanner

_PLANNER_FACTORIES = {
    "stub": StubPlanner,
    "openai": OpenAIPlanner,   # <-- new id
}
```

### 3. Declare it in config

```yaml
planner:
  active: openai
  providers:
    stub:   { enabled: true,  type: planner, description: "..." }
    openai: { enabled: true,  type: planner, description: "OpenAI planner" }
```

That's it. `runtime.start()` will build and register it; the menu will list
and toggle it.

## Where each future module lands

| Module                | Role folder           | Config section | Registry                    |
|-----------------------|-----------------------|----------------|-----------------------------|
| `OpenAIPlanner`       | `corax/planner/`      | `planner`      | `ProviderRegistry`          |
| `TelegramConnector`   | `corax/connectors/`   | `connectors`   | `ConnectorRegistry`         |
| `SQLiteMemory`        | `corax/memory/`       | `memory`       | `MemoryRegistry`            |
| `FilesystemCapability`| SDK package + `loader`| `capabilities` | `CapabilityRegistryAdapter` |
| `MCPAdapter`          | SDK package + `loader`| `capabilities` | `CapabilityRegistryAdapter` |

## Capabilities: in-tree vs. packages

- Small, dependency-free tools (like `echo`) live in `corax/capabilities/` and
  go in the `_CAPABILITY_FACTORIES` table.
- Richer tools ship as standalone **SDK packages** with a root
  `capability.json`. Add the path to `capabilities.available.<id>.path` in
  config; `corax/loader/capabilities.py` loads and validates them. No factory
  entry needed.

Because SDK packages are real `agent_core.Capability` instances, they can be
executed through the **agent-core kernel**: `corax/loader/core.py` (`CoreEngine`)
assembles the executor on demand and `runtime.execute("<capability id>",
input={...})` routes one task through route → policy → execute. The built-in
`echo` placeholder is *not* an `agent_core.Capability`, so the kernel skips it.

## Guardrails for file / shell capabilities

Before touching the filesystem, call `paths.is_blocked_path(config, target,
base)` and honour `security.allow_file_write` / `security.allow_shell`. Never
write under `security.blocked_paths` — that list includes `corax-core` and
`corax-sdk`, which must stay untouched.

## Do **not** in this stage

Implement Telegram, OpenAI, MCP or persistent memory; or modify `corax-core` /
`corax-sdk`. This stage is the scaffold plus the workspace-confined filesystem,
editor and shell capability packages.
