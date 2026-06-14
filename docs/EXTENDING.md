# Extending Corax

Every future module plugs into an existing seam. You should never need to
restructure the package to add one.

## The extension points

1. **config** — declare the provider/capability in `agent.yaml`.
2. **registry** — the runtime registers it under the matching role.
3. **factory table** — map the config id to your class in `runtime.py`.
4. **(optionally) settings** — extra fields are read via `get_setting`.

## Recipe: add a real provider

Say you want an `OpenAIPlanner`.

### 1. Implement it

Create `corax_agent/providers/openai_planner.py` with the same shape the
runtime expects of a planner (an `async plan(...)`, `async health()`, an `id`):

```python
class OpenAIPlanner:
    id = "planner.openai"
    kind = "planner"
    def __init__(self, **opts): ...
    async def plan(self, goal, *, correlation_id=None): ...
    async def health(self): ...
```

> Keep secrets out of the repo — read them from env at construction.

### 2. Register it in the factory table

In `corax_agent/runtime.py`:

```python
from corax_agent.providers.openai_planner import OpenAIPlanner

_PLANNER_FACTORIES = {
    "stub": PlannerStub,
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

| Module                | Config section | Registry                    |
|-----------------------|----------------|-----------------------------|
| `TelegramConnector`   | `connectors`   | `ConnectorRegistry`         |
| `OpenAIPlanner`       | `planner`      | `ProviderRegistry`          |
| `SQLiteMemory`        | `memory`       | `MemoryRegistry`            |
| `MCPAdapter`          | `capabilities` | `CapabilityRegistryAdapter` |
| `FilesystemCapability`| `capabilities` | `CapabilityRegistryAdapter` |
| `ShellCapability`     | `capabilities` | `CapabilityRegistryAdapter` |

## Guardrails for file / shell capabilities

Before touching the filesystem, call `paths.is_blocked_path(config, target,
base)` and honour `security.allow_file_write` / `security.allow_shell`. Never
write under `security.blocked_paths` — that list includes `corax-core` and
`corax-sdk`, which must stay untouched.

## Do **not** in this stage

Implement Telegram, OpenAI, MCP, shell execution, file tools or persistent
memory; or modify `corax-core` / `corax-sdk`. This stage is the scaffold only.
