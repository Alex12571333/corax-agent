#!/usr/bin/env python3
"""Corax Agent — CLI entrypoint.

Usage:
    python main.py                 # open the settings menu (default)
    python main.py --menu          # open the settings menu
    python main.py --status        # print runtime status and exit
    python main.py --init          # create config + workspace/data/logs and exit
    python main.py --config PATH    # use an explicit config file
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from corax import config as config_mod
from corax.app import CoraxApp
from corax.paths import default_config_path, ensure_paths
from corax.ui.banner import BANNER


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corax-agent",
        description="Corax Agent — minimal agent scaffold.",
    )
    parser.add_argument("--menu", action="store_true", help="open the settings menu (default)")
    parser.add_argument("--status", action="store_true", help="print runtime status and exit")
    parser.add_argument("--chat", action="store_true", help="run the Telegram gateway (connectors routed through the core kernel)")
    parser.add_argument("--init", action="store_true", help="create config and directories, then exit")
    parser.add_argument("--config", metavar="PATH", help="path to the config file (yaml or json)")
    return parser


def _resolve_config_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    return default_config_path(Path.cwd())


async def _run(args: argparse.Namespace) -> int:
    config_path = _resolve_config_path(args.config)

    if args.init:
        return _do_init(config_path)

    app = CoraxApp(config_path)
    await app.boot()
    try:
        if args.status:
            status = await app.runtime.status()
            print("\nCorax runtime status\n")
            print(status.render())
            print()
        elif args.chat:
            return await _run_chat(app, config_path)
        else:
            await app.run_menu()
    finally:
        await app.shutdown()
    return 0


def _tool_capability_specs(runtime) -> list[dict]:
    """Describe the kernel-executable capabilities as tool specs for the model."""
    from corax.loader.core import _as_pairs

    specs: list[dict] = []
    for cap_id, item in _as_pairs(runtime.capabilities):
        if not runtime.core.is_executable(item):
            continue
        specs.append(
            {
                "id": cap_id,
                "description": getattr(item, "description", "") or "",
                "input_schema": getattr(item, "input_schema", {}) or {},
            }
        )
    return specs


async def _run_chat(app: "CoraxApp", config_path: Path) -> int:
    """Run the Telegram gateway as an agent: the model can call every capability
    through the agent-core kernel (tool-calling), with results fed back to it.
    """
    runtime = app.runtime
    if not runtime.core.available:
        print("agent-core is not installed; --chat needs the execution kernel.")
        return 1
    if not (os.getenv("CORAX_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")):
        print("Set CORAX_TELEGRAM_BOT_TOKEN before running --chat.")
        return 1
    if "llm.local" not in runtime.capabilities:
        print("llm.local capability is not loaded; cannot run the chat.")
        return 1

    from corax.gateway import CoraxTelegramGateway
    from corax.gateway.policy import GatewayPolicyEngine

    specs = _tool_capability_specs(runtime)
    tool_ids = [s["id"] for s in specs if s["id"] not in ("llm.local", "telegram.connector")]
    if not app.config.telegram.allowed_chats:
        print(
            "⚠️  SECURITY: no CORAX_TELEGRAM_ALLOWED_CHATS set — anyone who can "
            "message the bot can drive these tools (incl. shell). Set an allow-list."
        )
    print(f"Tools exposed to the model: {', '.join(tool_ids) or '(none)'}")

    while True:
        async with runtime.core.session(
            runtime.capabilities, policy=GatewayPolicyEngine()
        ) as kernel:
            gateway = CoraxTelegramGateway(
                run_capability=kernel.invoke,
                capabilities=specs,
                model=app.config.llm.model,
            )
            print("Corax Telegram gateway is running (Ctrl-C to stop).")
            outcome = await gateway.run()

        if outcome == "reload":
            print("Reloading agent…")
            await runtime.reload_config(config_mod.load_config(config_path))
            specs = _tool_capability_specs(runtime)
            continue
        return 0


def _do_init(config_path: Path) -> int:
    existed = config_path.exists()
    config = config_mod.load_config(config_path) if existed else config_mod.create_default_config(config_path)
    paths = ensure_paths(config, config_path)
    print(BANNER.rstrip("\n"))
    print()
    if existed:
        print(f"Config already present: {config_path}")
    else:
        print(f"Created default config: {config_path}")
    print("Ensured directories:")
    print(f"  workspace : {paths.workspace}")
    print(f"  data      : {paths.data}")
    print(f"  logs      : {paths.logs}")
    print()
    errors = config_mod.validate_config(config)
    if errors:
        print("Config warnings:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("Config is valid.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
