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
import uuid
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


async def _run_chat(app: "CoraxApp", config_path: Path) -> int:
    """Run the Telegram gateway: route connectors through the agent-core kernel.

    Every connector call (poll, send, the live streaming edits) goes through the
    kernel under a permissive gateway policy; only the LLM token stream itself is
    read straight from the ``llm.local`` instance (the one-shot kernel cannot
    stream tokens).
    """
    runtime = app.runtime
    if not runtime.core.available:
        print("agent-core is not installed; --chat needs the execution kernel.")
        return 1
    if not (os.getenv("CORAX_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")):
        print("Set CORAX_TELEGRAM_BOT_TOKEN before running --chat.")
        return 1

    from agent_core import CapabilityRequest, TaskStatus

    from corax.gateway import CoraxTelegramGateway, GatewayError
    from corax.gateway.policy import GatewayPolicyEngine

    llm = runtime.capabilities.get("llm.local")
    if llm is None or not hasattr(llm, "stream_generate"):
        print("llm.local capability is not loaded; cannot stream replies.")
        return 1

    wait_timeout = 90.0

    while True:
        async with runtime.core.session(
            runtime.capabilities, policy=GatewayPolicyEngine()
        ) as kernel:

            async def run_capability(cap_id: str, payload: dict, *, session_id: str | None = None) -> dict:
                sid = session_id or f"gw-{uuid.uuid4().hex[:8]}"
                request_input = dict(payload)
                request_input["state_key"] = "gw_output"
                task = await kernel.run_task(
                    required_capability=cap_id,
                    input=request_input,
                    session_id=sid,
                    wait_timeout=wait_timeout,
                )
                if task.status is not TaskStatus.COMPLETED:
                    raise GatewayError(f"{cap_id} task ended {task.status.value}")
                state = await kernel.get_state(sid)
                return dict(state.temporary_context.get("gw_output") or {})

            async def stream_llm(payload: dict, *, session_id: str):
                request = CapabilityRequest(
                    task_id=f"gw-{uuid.uuid4().hex[:8]}",
                    session_id=session_id,
                    input=payload,
                )
                async for chunk in llm.stream_generate(request):
                    yield chunk

            gateway = CoraxTelegramGateway(
                run_capability=run_capability,
                stream_llm=stream_llm,
                model=app.config.llm.model,
            )
            print("Corax Telegram gateway is running (Ctrl-C to stop).")
            outcome = await gateway.run()

        if outcome == "reload":
            print("Reloading agent…")
            await runtime.reload_config(config_mod.load_config(config_path))
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
