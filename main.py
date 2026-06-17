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
import signal
import sys
from pathlib import Path
from typing import Any

from corax import config as config_mod
from corax.app import CoraxApp
from corax.paths import default_config_path, ensure_paths
from corax.ui.banner import BANNER

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


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


def _chat_system_prompt(root_path: str | Path) -> str | None:
    """Load the operator-editable chat prompt files when present."""
    prompt_dir = Path(root_path) / "prompts"
    parts: list[str] = []
    for name in ("system.md", "safety.md"):
        path = prompt_dir / name
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            parts.append(text)
    return "\n\n---\n\n".join(parts) if parts else None


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
    from corax.tool_discovery import RuntimeToolSelector

    specs = _tool_capability_specs(runtime)
    selector = RuntimeToolSelector(app.config, root_path=runtime.root_path)
    stream_transport = _telegram_stream_transport()
    system_prompt = _chat_system_prompt(runtime.root_path)
    tool_ids = [
        s["id"]
        for s in specs
        if s["id"] not in ("gateway", "llm.local", "telegram.connector")
    ]
    if not app.config.telegram.allowed_chats:
        _print_warning(
            "SECURITY",
            "no CORAX_TELEGRAM_ALLOWED_CHATS set; anyone who can message the bot can drive these tools.",
        )
    _print_chat_dashboard(
        app,
        specs,
        tool_ids,
        tool_discovery=selector.available,
        stream_transport=stream_transport,
    )

    while True:
        async with runtime.core.session(
            runtime.capabilities, policy=GatewayPolicyEngine()
        ) as kernel:
            gateway_kwargs = {
                "run_capability": kernel.invoke,
                "stream_capability": kernel.stream_generate_events,
                "capabilities": specs,
                "model": app.config.llm.model,
                "workspace_path": runtime.workspace_path,
                "tool_selector": selector.select if selector.available else None,
                "stream_transport": stream_transport,
            }
            if system_prompt is not None:
                gateway_kwargs["system_prompt"] = system_prompt
            gateway = CoraxTelegramGateway(**gateway_kwargs)
            print(_style("Corax Telegram gateway is running. Ctrl-C to stop.", _GREEN))
            outcome = await _run_gateway_until_stopped(gateway)

        if outcome == "reload":
            print(_style("Reloading agent...", _YELLOW))
            await runtime.reload_config(config_mod.load_config(config_path))
            specs = _tool_capability_specs(runtime)
            selector = RuntimeToolSelector(app.config, root_path=runtime.root_path)
            stream_transport = _telegram_stream_transport()
            system_prompt = _chat_system_prompt(runtime.root_path)
            tool_ids = [
                s["id"]
                for s in specs
                if s["id"] not in ("gateway", "llm.local", "telegram.connector")
            ]
            _print_chat_dashboard(
                app,
                specs,
                tool_ids,
                tool_discovery=selector.available,
                stream_transport=stream_transport,
            )
            continue
        return 0


def _telegram_stream_transport() -> str:
    transport = os.getenv("CORAX_TELEGRAM_STREAM_TRANSPORT", "edit").strip().lower()
    if transport in {"auto", "draft", "edit", "off"}:
        return transport
    _print_warning(
        "STREAMING",
        f"invalid CORAX_TELEGRAM_STREAM_TRANSPORT={transport!r}; using edit.",
    )
    return "edit"


async def _run_gateway_until_stopped(gateway: Any) -> str:
    """Run the gateway with a graceful Ctrl-C path.

    Telegram long-poll uses a blocking HTTPS read inside the connector. Raising
    KeyboardInterrupt once breaks that read; the connector turns it into a
    regular failed poll, and the gateway exits because ``stop()`` was already
    set. A second Ctrl-C is treated as the user's request to force termination.
    """
    previous_handler = signal.getsignal(signal.SIGINT)
    interrupts = 0

    def _handle_sigint(_signum: int, _frame: Any) -> None:
        nonlocal interrupts
        interrupts += 1
        gateway.stop()
        if interrupts == 1:
            print()
            print(_style("Stopping Telegram gateway...", _YELLOW))
        else:
            raise KeyboardInterrupt
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, _handle_sigint)
    except (ValueError, RuntimeError):
        return await gateway.run()
    try:
        return await gateway.run()
    except KeyboardInterrupt:
        gateway.stop()
        return "stopped"
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def _print_chat_dashboard(
    app: "CoraxApp",
    specs: list[dict],
    tool_ids: list[str],
    *,
    tool_discovery: bool = False,
    stream_transport: str = "edit",
) -> None:
    runtime = app.runtime
    executable = runtime.core.executable_ids(runtime.capabilities)
    has_gateway = any(spec["id"] == "gateway" for spec in specs)
    has_telegram = any(spec["id"] == "telegram.connector" for spec in specs)
    allowed_chats = app.config.telegram.allowed_chats.strip() or "not set"
    rows = [
        ("mode", "telegram chat gateway"),
        ("model", app.config.llm.model),
        ("kernel", f"ready, {len(executable)} executable capability(ies)"),
        ("gateway", "standalone capability" if has_gateway else "fallback in-process memory"),
        ("connector", "telegram.connector" if has_telegram else "missing"),
        ("streaming", f"{stream_transport} transport"),
        ("tool mode", "dynamic top-K selector" if tool_discovery else "static full list"),
        ("tools", ", ".join(tool_ids) or "none"),
        ("allowed chats", allowed_chats),
        ("workspace", str(runtime.workspace_path)),
    ]

    print()
    print(_style("Corax Chat Gateway", _BOLD + _CYAN))
    print(_style("-" * 64, _DIM))
    for label, value in rows:
        print(f"{_style(label.rjust(13), _DIM)}  {_style(value, _GREEN if label in {'kernel', 'gateway'} else '')}")
    print(_style("-" * 64, _DIM))


def _print_warning(title: str, message: str) -> None:
    print()
    print(_style(f"! {title}", _YELLOW + _BOLD))
    print(_style(f"  {message}", _YELLOW))


def _style(text: str, color: str) -> str:
    if not color or not _color_enabled():
        return text
    return f"{color}{text}{_RESET}"


def _color_enabled() -> bool:
    mode = os.getenv("CORAX_COLOR", "auto").strip().lower()
    if mode in {"1", "true", "yes", "always", "on"}:
        return True
    if mode in {"0", "false", "no", "never", "off"}:
        return False
    return sys.stdout.isatty()


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
