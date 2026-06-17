# Corax Agent

A **minimal agent scaffold** — runtime, configuration, a terminal settings
menu, registries and clean extension points. This is the *landing site* for a
real agent: memory, connectors, capabilities and an LLM planner all plug in
later **without changing this structure**.

> This stage is intentionally inert. The planner, memory and connectors are
> built-in placeholders; the only real tools are the workspace-confined
> filesystem, editor and shell capability packages. No LLM, Telegram or MCP yet
> — those arrive through the registries.

## What it does today

1. Starts and stops cleanly.
2. Reads / writes config (`corax.yaml`, JSON fallback).
3. Shows a terminal settings menu and persists settings.
4. Builds a runtime from built-in components plus standalone SDK capabilities.
5. Loads `filesystem`, `editor` and `shell` capabilities from sibling repos.
6. Lists capabilities / connectors / memory / providers from config.
7. When `agent-core` is installed, runs tasks through the real execution
   kernel (`runtime.execute(...)`); otherwise degrades gracefully.
8. Exposes clear, role-based extension points for future modules.

## Requirements

* Python **3.11+**.
* Optional: `pyyaml` for full YAML fidelity. Without it, the scaffold uses the
  built-in minimal YAML reader/writer (`corax/yaml_lite.py`), or a `corax.json`
  config.
* The capability **packages** (`filesystem`, `editor`, `shell`) need
  `agent-sdk` / `agent-core`. The scaffold itself runs without them — those
  capabilities are simply skipped with a warning.

```bash
pip install -e ".[yaml,dev]"
```

## Usage

```bash
corax setup                 # first-run/settings menu
corax gateway               # run the Telegram chat gateway
corax status                # print runtime status and exit
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
│   ├── registry/           # capabilities · connectors · memory · providers
│   ├── loader/             # agent-sdk capability packages + agent-core kernel
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

## Capability integration

The default `corax.yaml` enables:

* `echo` — built-in, returns its input unchanged
* `filesystem` from `../corax-filesystem-capability`
* `editor` from `../corax-editor-capability`
* `shell` from `../corax-shell-capability`

Each package is loaded by `corax/loader/capabilities.py` from its root
`capability.json` manifest and `main.py` entrypoint. The runtime passes the
agent workspace to filesystem and editor so their sandbox is the same
`workspace/` directory the agent manages.

## Tests

```bash
# stdlib only (run from the repo root)
python -m unittest discover -s tests -t .

# or with coverage
pytest --cov=corax
```

The capability-integration test runs only when `agent-core` / `agent-sdk` and
the sibling capability repos are present; otherwise it is skipped.

## What's next

These slot into the existing registries with no architectural change:
`TelegramConnector`, `OpenAIPlanner`, `SQLiteMemory`, `MCPAdapter`. See
[docs/EXTENDING.md](docs/EXTENDING.md).

## Related (existing, do not modify)

* `corax-core` — https://github.com/Alex12571333/agent-core
* `corax-sdk`  — https://github.com/Alex12571333/agent-sdk

These are referenced as read-only and are listed in `security.blocked_paths`.
