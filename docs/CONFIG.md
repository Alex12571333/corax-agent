# Configuration

Config is stored as `corax.yaml` (or `corax.json` if you prefer / lack PyYAML).
It is loaded into a tree of dataclasses (`corax/config.py`) and mutated
only through `corax/settings.py`.

## Format selection

* `*.yaml` / `*.yml` → YAML. Uses **PyYAML** if installed; otherwise a built-in
  minimal block-YAML reader/writer that covers exactly this file's shape.
* `*.json` → JSON (stdlib).

`default_config_path()` looks for `corax.yaml`, `corax.yml`, `corax.json`, then
the legacy `agent.yaml`, `agent.yml`, `agent.json`, `config.json` in order, and
defaults to `corax.yaml` (or `corax.json` when PyYAML is absent).

## Sections

| Section        | Key fields                                                                 |
|----------------|----------------------------------------------------------------------------|
| `agent`        | `name`, `profile`, `mode`, `first_run`                                      |
| `runtime`      | `autostart`, `execution_mode`, `log_level`, `workspace_path`, `data_path`, `logs_path` |
| `extensions`   | `active{kind: ids[]}`, `bindings{role:id}`, `available{id:spec}`              |
| `security`     | `mode` (initial policy mode), `blocked_paths[]`                              |
| `limits`       | `max_parallel_tasks`, `max_plan_tasks`, `max_tasks_per_correlation`, `task_timeout_seconds`, `max_payload_mb` |
| `ui`           | `theme`, `mascot`, `show_banner`                                            |
| `prompts`      | prompt root, identity paths, and per-layer/total character budgets          |
| `tool_routing` | embedding endpoint/model, top-K, similarity and schema budgets              |

`extensions` is the only activation catalogue. `bindings` select one provider
for scalar roles; `active` contains the loaded ids for every extension kind.

`tool_routing` defaults to the local OpenAI-compatible embedding service at
`http://192.168.0.10:8080/v1` with
`nvidia/Nemotron-3-Embed-1B-NVFP4` (2048 dimensions). It is independent from
the generation model. `CORAX_EMBEDDING_BASE_URL` and
`CORAX_EMBEDDING_MODEL` may override those two values without editing the
file. No LLM reranker is used.

`prompts` selects the layered Markdown runtime. Package defaults are immutable;
operator overrides and `USER.md` / `MEMORY.md` live under the configured
runtime/data roots. `corax prompts validate` checks required UTF-8 layers and
budgets without printing private content.

`runtime.execution_mode` accepts `object` (default) or `legacy`. Object mode is
effective only for console/TUI when its prompt runtime, state binding, object
store, and Docker Python runner are healthy; otherwise Corax uses the legacy
tool loop. `corax doctor` reports this gate.

`object.runtime` is enabled and bound as a host-only runtime service. JSON tool
results larger than `CORAX_OBJECT_INLINE_BYTES` (default `3000`) are stored
under `CORAX_OBJECT_PATH` and represented in model history by a bounded
session-owned reference. `CORAX_OBJECT_MAX_BYTES` limits one object,
`CORAX_OBJECT_STORE_MAX_BYTES` limits the store,
`CORAX_OBJECT_TTL_SECONDS` controls expiry, and
`CORAX_OBJECT_PREVIEW_CHARS` bounds redacted previews. Task workspaces use the
selected `state` binding. The runner uses only an already installed Docker
image named by `CORAX_SANDBOX_DOCKER_IMAGE`; Corax resolves it to a digest and
never pulls it implicitly.

Set standard Docker `DOCKER_HOST=ssh://user@host` to run the isolated Python
container on a Docker daemon reachable directly over the LAN. No Docker TCP
API or host bind mount is required.

Tool extensions may add an optional `routing` object to `extension.json`.
Useful fields are `title`, `summary`, `domains`, `tags`, `intents`,
`examples`, `anti_examples`, `operations`, `channels`, `always_available`,
and `cost`. Old manifests remain valid: missing routing fields fall back to
the loaded tool description, tags, schema operations, and security metadata.
Changing only an input schema invalidates its schema hash; changing the compact
routing card causes only that tool to be re-embedded.

## Settings API

```python
from corax import settings

settings.get_setting(config, "runtime.autostart")             # -> True
settings.set_setting(config, "runtime.log_level", "DEBUG")    # coerces by type
settings.toggle_provider(config, "planner", "stub", True)     # enable/disable
settings.set_active_provider(config, "memory", "none")        # set/append active
settings.deactivate_provider(config, "connectors", "terminal")# remove from list
```

`set_setting` coerces the string from the menu to the existing field's type
(bool / int / float / list / str). Disabling a provider also removes it from
any active/enabled list so the config stays consistent.

## Validation

`validate_config(config) -> list[str]` returns human-readable errors (empty =
valid). It checks: required sections, `log_level`, `security.mode`, that active
extension ids and role bindings exist, have the right kind, and are enabled,
and that runtime, prompt, and tool-routing limits are valid. `corax init` runs it and
reports warnings.

`security.mode` accepts `ask`, `auto`, or `full`. Old `normal`, `strict`, and
`paranoid` values are migrated to the conservative `ask` mode. The selected
`policy_provider` persists live command changes in the runtime data directory;
the config value is its initial mode. `full` changes policy decisions only:
immutable deny rules and the actual filesystem/shell/OS boundaries remain.

## Editing via the menu

`corax settings` opens a menu with sections for Runtime, Planner, Memory,
Connectors, Capabilities, Security, Limits and Paths. "Save and Exit" writes
the file; "Exit without saving" discards in-memory changes.
