# Extending Corax

Corax uses typed extension packages. Choose the runtime role first; do not put
every callable component into the tool registry.

## Choose the kind

| What the package does | `kind` | Core contract |
| --- | --- | --- |
| The LLM may directly choose it | `tool` | `ToolCapability.execute` |
| Receives or sends channel messages | `channel_connector` | `ChannelConnector.receive/send` |
| Calls or hosts a model | `model_provider` | `ModelProvider.generate` |
| Stores or recalls long-lived memory | `memory_provider` | `MemoryProvider.remember/recall/forget` |
| Makes authorization decisions | `policy_provider` | `PolicyProvider.evaluate` |
| Runs internal orchestration | `runtime_service` | `RuntimeService.handle` |
| Implements persistence, telemetry or translation | `storage_provider`, `observability`, `adapter` | the matching typed contract |

Only `tool` packages use `exposure: agent` and appear in the LLM tool list.
Every other kind uses `exposure: runtime` and is resolved by its role or a
named binding. Security is a cross-cutting policy checkpoint, not a connector.

## Package contract

Every external package contains:

```text
my-extension/
  extension.json
  main.py
  README.md
  pyproject.toml
  tests/
```

Use the matching SDK decorator (`@tool`, `@channel_connector`,
`@model_provider`, `@memory_provider`, `@runtime_service` or `@adapter`) and
the matching Agent Core base class. The manifest and class must agree on:

- stable `id`, `kind`, `interfaces` and entrypoint;
- permissions, scopes, risk, side effects and secret names;
- config, request and response schemas;
- Core/SDK compatibility.

`capability.json`, `@capability` and `Capability` are migration-only APIs.

## Add a package to Corax

Declare it in `extensions.available` and activate it under the matching kind:

```yaml
extensions:
  active:
    tool: [echo, filesystem, editor, shell, web.search]
    channel_connector: [terminal, telegram.connector]
    model_provider: [stub, llm.local]
    memory_provider: [memory.none]
    runtime_service: [gateway]
  bindings:
    primary_model: llm.local
    planner: stub
    memory: memory.none
  available:
    web.search:
      kind: tool
      path: ../corax-web-search-capability
      enabled: true
```

`ExtensionLoader` reads `extension.json`, validates compatibility, loads the
entrypoint, and verifies that the instance implements the declared role.
`ExtensionCatalog` then registers it in the corresponding role registry.

## Runtime use

Tools run through the Agent Core policy/execution kernel:

```python
task = await runtime.execute(
    "filesystem",
    input={"operation": "read", "path": "notes.txt"},
)
```

Infrastructure is invoked directly through its typed runtime contract:

```python
reply = await runtime.invoke_extension(
    "llm.local",
    {"prompt": "Summarize the result."},
)
```

The gateway uses the same dispatcher, but only the tool registry is converted
to model-facing tool specifications.

## Definition of done

- The kind describes the actual role, not merely “has callable methods”.
- The manifest passes `agent-sdk extension validate .`.
- Discovery works without importing package code.
- Loader tests prove the class/manifest role match.
- Lifecycle and health checks are covered.
- Local resource validation and structured error handling are covered.
- Secrets are injected by name and never stored in manifests or results.
- The README documents configuration, security, operations and non-goals.

See the Agent SDK extension specification for the complete manifest format.
