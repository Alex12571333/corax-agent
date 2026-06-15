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
| `runtime`      | `autostart`, `log_level`, `workspace_path`, `data_path`, `logs_path`        |
| `planner`      | `active`, `providers{ id: {enabled,type,description} }`                      |
| `memory`       | `active`, `providers{…}`                                                     |
| `connectors`   | `active[]`, `providers{…}`                                                   |
| `capabilities` | `enabled[]`, `available{…}`                                                  |
| `security`     | `mode`, `core_readonly`, `allow_shell`, `allow_file_write`, `blocked_paths[]`|
| `limits`       | `max_parallel_tasks`, `max_plan_tasks`, `max_tasks_per_correlation`, `task_timeout_seconds`, `max_payload_mb` |
| `ui`           | `theme`, `mascot`, `show_banner`                                            |

`planner.active` / `memory.active` are **single ids**; `connectors.active` and
`capabilities.enabled` are **lists**.

## Settings API

```python
from corax import settings

settings.get_setting(config, "security.allow_shell")          # -> False
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
planner/memory/connectors/capabilities exist and are enabled, and that limits
are positive integers. `python main.py --init` runs it and reports warnings.

## Editing via the menu

`python main.py` opens a menu with sections for Runtime, Planner, Memory,
Connectors, Capabilities, Security, Limits and Paths. "Save and Exit" writes
the file; "Exit without saving" discards in-memory changes.
