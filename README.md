# Corax Agent

A typed agent runtime with configuration, lifecycle, a terminal settings menu,
an execution kernel and role-specific extension registries. Tools, channels,
models, memory and runtime services share one package format without sharing
one execution contract.

## What it does today

1. Starts and stops cleanly.
2. Reads / writes config (`corax.yaml`, JSON fallback).
3. Shows a terminal settings menu and persists settings.
4. Discovers and validates standalone `extension.json` packages.
5. Registers each package by kind: tools, channels, models, memory, policy or services.
6. Exposes only tools to the planner and Agent Core execution kernel.
7. Invokes infrastructure through its typed runtime contract.
8. Supports the local model, Telegram channel, gateway and memory adapters
   without placing them in the tool list.

## Requirements

* Python **3.11+**.
* Optional: `pyyaml` for full YAML fidelity. Without it, the scaffold uses the
  built-in minimal YAML reader/writer (`corax/yaml_lite.py`), or a `corax.json`
  config.
* External extension packages need
  `agent-sdk` / `agent-core`. The scaffold itself runs without them — those
  packages are simply skipped with a warning.

```bash
pip install -e ".[yaml,dev]"
```

## Usage

```bash
corax setup                 # first-run/settings menu
corax gateway               # run the Telegram chat gateway
corax status                # print runtime status and exit
corax security status       # show ask / auto / full
corax security mode auto    # switch permission mode
corax init                  # create config + workspace/data/logs and exit
corax --config ./corax.yaml setup
```

Legacy development aliases still work (`python main.py --chat`,
`python main.py --status`, `python main.py --init`), but the public CLI shape is
`corax <command>`.

## Project layout

```
corax-agent/
├── main.py                 # CLI entrypoint
├── corax.yaml              # default config
├── corax/
│   ├── app.py              # boot / shutdown / run_menu
│   ├── runtime.py          # CoraxRuntime + RuntimeStatus
│   ├── config.py           # dataclasses + load/save/validate
│   ├── settings.py         # get/set/toggle/activate (the only config mutator)
│   ├── paths.py            # path resolution + blocked-path guard
│   ├── logging.py          # console + file logging
│   ├── yaml_lite.py        # minimal YAML reader/writer (PyYAML-optional)
│   ├── health.py           # uniform Health payload
│   ├── ui/                 # menu · terminal · screens · banner
│   ├── registry/           # role-specific extension registries
│   ├── loader/             # agent-sdk extension packages + agent-core kernel
│   ├── planner/            # StubPlanner (built-in)
│   ├── connectors/         # TerminalConnector (built-in)
│   ├── memory/             # NullMemory (built-in)
│   └── capabilities/       # EchoCapability (built-in)
├── prompts/                # system · planner · safety (templates)
├── docs/                   # ARCHITECTURE · CONFIG · EXTENDING
├── tests/                  # unittest suite (pytest-compatible)
└── workspace/  data/  logs/
```

Code is grouped **by role** — a real `OpenAIPlanner` lands in `corax/planner/`
next to `StubPlanner`, a `TelegramConnector` in `corax/connectors/`, and so on.

## Extension integration

The default `corax.yaml` groups packages under `extensions.active`:

- tools: `echo`, `filesystem`, `editor`, `shell`, `web.search`;
- channels: `terminal`, `telegram.connector`;
- models: `stub`, `llm.local`;
- memory: `memory.none`;
- policy: `security.policy`;
- services: `gateway`.

Each external package is loaded from its root `extension.json`. The runtime
validates that the entrypoint implements the declared kind before adding it to
the corresponding registry. Only tools pass through `runtime.execute(...)`.
The selected policy is injected into every Agent Core session and is never
model-callable.

Security mode can also be controlled by an authorised Telegram operator:

```text
/security status
/security mode ask
/security mode auto
/security mode full
/security approve <task-id>
/security deny <task-id>
```

Entering `full` requires repeating the command with the returned short
challenge. Configure remote administrators with
`CORAX_SECURITY_ADMIN_IDS=12345,67890`.

## Tests

```bash
# stdlib only (run from the repo root)
python -m unittest discover -s tests -t .

# or with coverage
pytest --cov=corax
```

Integration tests exercise every sibling package when the repositories are
present.

## What's next

See [docs/EXTENDING.md](docs/EXTENDING.md) for the developer contract and the
rule for choosing an extension kind.

## Related (existing, do not modify)

* `corax-core` — https://github.com/Alex12571333/agent-core
* `corax-sdk`  — https://github.com/Alex12571333/agent-sdk

These are referenced as read-only and are listed in `security.blocked_paths`.
