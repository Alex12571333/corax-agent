# Corax Agent

A **minimal agent scaffold** — runtime, configuration, a terminal settings
menu, registries and clean extension points. This is the *landing site* for a
real agent: memory, connectors, capabilities and an LLM planner all plug in
later **without changing this structure**.

> This stage is intentionally inert. It does **not** talk to an LLM, Telegram,
> MCP, a shell, the filesystem, or a real memory store. Those arrive later
> through the registries.

## What it does today

1. Starts and stops cleanly.
2. Reads / writes config (`agent.yaml`, JSON fallback).
3. Shows a terminal settings menu.
4. Persists settings.
5. Builds a runtime from stubs plus standalone SDK capability packages.
6. Loads `filesystem`, `editor`, and `shell` capabilities from sibling repos.
7. Lists capabilities / connectors / memory / providers from config.
8. Exposes clear extension points (registries) for future modules.

## Requirements

* Python **3.11+** (uses `tomllib`-era stdlib; no hard third-party deps).
* Optional: `pyyaml` for full YAML fidelity. Without it, the scaffold uses a
  built-in minimal YAML reader/writer, or you can use a `agent.json` config.

```bash
# optional extras
pip install -e ".[yaml,dev]"
```

## Usage

```bash
python main.py            # open the settings menu (default)
python main.py --menu     # open the settings menu
python main.py --status   # print runtime status and exit
python main.py --init     # create config + workspace/data/logs and exit
python main.py --config ./agent.yaml
```

## Capability integration

The default `agent.yaml` enables:

* `filesystem` from `../corax-filesystem-capability`
* `editor` from `../corax-editor-capability`
* `shell` from `../corax-shell-capability`

Each package is loaded through `agent-sdk` from its root `capability.json` and
`main.py` entrypoint. The runtime passes the agent workspace to filesystem and
editor so their sandbox is the same `workspace/` directory the agent manages.

### Acceptance checks

```bash
python main.py --init     # creates config + folders
python main.py --status   # shows runtime status + registered stubs
python main.py            # opens the settings menu
```

## Project layout

```
corax-agent/
├── main.py                 # CLI entrypoint
├── agent.yaml              # default config
├── corax_agent/
│   ├── app.py              # boot / shutdown / run_menu
│   ├── runtime.py          # CoraxRuntime + RuntimeStatus (stub-backed)
│   ├── config.py           # dataclasses + load/save/validate
│   ├── settings.py         # get/set/toggle/activate by key path
│   ├── menu.py             # terminal settings menu
│   ├── paths.py            # path resolution + blocked-path guard
│   ├── logging_setup.py    # console + file logging
│   ├── registries/         # connectors / memory / providers / capabilities
│   ├── stubs/              # planner / connector / memory / capability stubs
│   ├── ui/                 # terminal I/O + screen renderers
│   └── tests/              # unittest suite
├── workspace/  data/  logs/
└── docs/                   # ARCHITECTURE / CONFIG / EXTENDING
```

## Tests

```bash
# stdlib only
python -m unittest discover -s corax_agent/tests -v

# or with coverage (pip install pytest pytest-cov)
pytest --cov=corax_agent
```

## What's next

These slot into the existing registries with no architectural change:

* `TelegramConnector`, `OpenAIPlanner`, `SQLiteMemory`, `MCPAdapter`,
  `FilesystemCapability`, `ShellCapability`.

See [docs/EXTENDING.md](docs/EXTENDING.md).

## Related (existing, do not modify)

* `corax-core` — https://github.com/Alex12571333/agent-core
* `corax-sdk`  — https://github.com/Alex12571333/agent-sdk

These are referenced as read-only and are listed in `security.blocked_paths`.
```
