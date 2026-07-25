#!/usr/bin/env python3
"""Corax Agent — CLI entrypoint.

Usage:
    corax                          # first-run setup, then full-screen TUI
    corax tui                      # full-screen terminal chat
    corax chat                     # simple line-oriented console fallback
    corax setup                    # guided setup wizard
    corax settings                 # advanced settings menu
    corax gateway                  # run the Telegram gateway
    corax status                   # print runtime status and exit
    corax security status          # show the active permission mode
    corax security mode auto       # switch ask / auto / full
    corax observability            # show the local trace sink status
    corax eval                     # run deterministic ecosystem checks
    corax doctor                   # check local runtime readiness
    corax init                     # create config + workspace/data/logs and exit
    corax --config PATH setup      # use an explicit config file
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from corax_ui import TerminalTheme, safe_text

from corax import __version__ as CORAX_VERSION
from corax import config as config_mod
from corax.app import CoraxApp
from corax.paths import default_config_path, ensure_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corax",
        description="Corax Agent — local agent runtime, setup, and gateways.",
    )
    parser.add_argument("--config", metavar="PATH", help="path to the config file (yaml or json)")
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "tui",
            "chat",
            "setup",
            "settings",
            "gateway",
            "status",
            "security",
            "mcp",
            "skills",
            "hooks",
            "subagents",
            "sandbox",
            "models",
            "observability",
            "eval",
            "doctor",
            "init",
            "menu",
        ),
        help="command to run (default: full-screen terminal chat)",
    )
    parser.add_argument(
        "command_args",
        nargs="*",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--menu", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--status", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--chat", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--init", action="store_true", help=argparse.SUPPRESS)
    return parser


def _resolve_config_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    return default_config_path(Path.cwd())


async def _run(args: argparse.Namespace) -> int:
    config_path = _resolve_config_path(args.config)
    command = _resolve_command(args)

    if command == "init":
        return _do_init(config_path)

    first_run = not config_path.exists()
    if not first_run:
        first_run = config_mod.load_config(config_path).agent.first_run
    if _needs_setup(command, first_run):
        setup_result = await _run_setup_wizard(
            config_path,
            first_run=first_run,
        )
        if setup_result != 0:
            return setup_result
        if command == "setup":
            return 0

    app = CoraxApp(config_path)
    await app.boot()
    try:
        if command == "status":
            status = await app.runtime.status()
            _print_result("Runtime status", status.render())
        elif command == "security":
            security_command = " ".join(args.command_args) or "status"
            result = await app.runtime.security_control(security_command)
            _print_result("Security", result.get("message") or result)
            return 0 if result.get("ok") or result.get("challenge") else 2
        elif command == "mcp":
            manager_id = app.config.extensions.bindings.get("mcp", "mcp.manager")
            if not app.runtime.services.has(manager_id):
                print("mcp.manager is not loaded.")
                return 1
            operation = (
                "list_tools"
                if args.command_args and args.command_args[0] == "tools"
                else "status"
            )
            result = await app.runtime.invoke_extension(
                manager_id,
                {"operation": operation},
                session_id="mcp-control",
            )
            _print_result("MCP", result)
            return 0
        elif command == "skills":
            service_id = app.config.extensions.bindings.get(
                "skills", "skills.runtime"
            )
            if not app.runtime.services.has(service_id):
                print("skills.runtime is not loaded.")
                return 1
            requested = args.command_args[0] if args.command_args else "status"
            operation = requested if requested in {"list", "reload"} else "status"
            result = await app.runtime.invoke_extension(
                service_id,
                {"operation": operation},
                session_id="skills-control",
            )
            _print_result("Skills", result)
            return 0
        elif command == "hooks":
            service_id = app.config.extensions.bindings.get("hooks", "hooks.runtime")
            if not app.runtime.services.has(service_id):
                print("hooks.runtime is not loaded.")
                return 1
            requested = args.command_args[0] if args.command_args else "status"
            operation = "reload" if requested == "reload" else "status"
            result = await app.runtime.invoke_extension(
                service_id,
                {"operation": operation},
                session_id="hooks-control",
            )
            _print_result("Hooks", result)
            return 0
        elif command == "subagents":
            service_id = app.config.extensions.bindings.get(
                "subagents", "subagents.orchestrator"
            )
            if not app.runtime.services.has(service_id):
                print("subagents.orchestrator is not loaded.")
                return 1
            result = await app.runtime.invoke_extension(
                service_id,
                {"operation": "status"},
                session_id="subagents-control",
            )
            _print_result("Subagents", result)
            return 0
        elif command == "sandbox":
            service_id = app.config.extensions.bindings.get(
                "sandbox", "sandbox.executor"
            )
            if not app.runtime.services.has(service_id):
                print("sandbox.executor is not loaded.")
                return 1
            result = await app.runtime.invoke_extension(
                service_id,
                {"operation": "status"},
                session_id="sandbox-control",
            )
            _print_result("Sandbox", result)
            return 0
        elif command == "models":
            router = app.runtime.active_model_router()
            if router is None or not hasattr(router, "status"):
                print("model.router is not loaded.")
                return 1
            _print_result("Models", router.status())
            return 0
        elif command == "observability":
            provider = app.runtime.active_observability()
            if provider is None or not hasattr(provider, "status"):
                print("observability.jsonl is not loaded.")
                return 1
            _print_result("Observability", provider.status())
            return 0
        elif command == "eval":
            try:
                from corax_evals import run_evaluations
            except ImportError:
                print(
                    "corax-evals is not installed; install the 'dev' extra "
                    "or the corax-evals package."
                )
                return 1
            report = run_evaluations(
                app.runtime.root_path,
                app.runtime.root_path.parent,
            )
            _print_result("Evaluations", report.render())
            return 0 if report.ok else 1
        elif command == "doctor":
            return await _run_doctor(app)
        elif command == "gateway":
            return await _run_chat(app, config_path)
        elif command == "tui":
            return await _run_console_chat(app, use_tui=True)
        elif command == "chat":
            return await _run_console_chat(app)
        else:
            _print_setup_overview(app)
            await app.run_menu()
    finally:
        await app.shutdown()
    return 0


def _resolve_command(args: argparse.Namespace) -> str:
    """Resolve modern subcommands plus legacy flags into one command name."""
    if args.init:
        return "init"
    if args.chat:
        return "gateway"
    if args.status:
        return "status"
    if args.menu:
        return "settings"
    if args.command == "menu":
        return "settings"
    return args.command or "tui"


def _needs_setup(command: str, first_run: bool) -> bool:
    """Gate interactive entrypoints, while keeping diagnostics available."""

    return command == "setup" or (
        first_run and command in {"tui", "chat", "gateway"}
    )


async def _run_doctor(app: "CoraxApp") -> int:
    """Report local composition readiness without making network requests."""

    runtime = app.runtime
    config_errors = config_mod.validate_config(app.config)
    model_id = app.config.extensions.bindings.get("primary_model", "")
    checks = [
        ("config", not config_errors, "; ".join(config_errors) or "valid"),
        (
            "agent-core",
            runtime.core.available,
            "available" if runtime.core.available else "missing",
        ),
        (
            "primary model",
            bool(model_id and runtime.models.has(model_id)),
            model_id or "not configured",
        ),
        (
            "security policy",
            runtime.active_policy() is not None,
            app.config.extensions.bindings.get("policy", "not configured"),
        ),
        (
            "sandbox",
            runtime.active_sandbox_executor() is not None,
            app.config.extensions.bindings.get("sandbox", "not configured"),
        ),
        (
            "state",
            runtime.active_state_store() is not None,
            app.config.extensions.bindings.get("state", "not configured"),
        ),
        (
            "observability",
            runtime.active_observability() is not None,
            app.config.extensions.bindings.get(
                "observability",
                "not configured",
            ),
        ),
        (
            "workspace",
            runtime.workspace_path.is_dir()
            and os.access(runtime.workspace_path, os.R_OK | os.W_OK),
            str(runtime.workspace_path),
        ),
    ]
    passed = sum(ok for _, ok, _ in checks)
    theme = _theme()
    print(theme.header(f"Corax doctor · {passed}/{len(checks)} passed"))
    for name, ok, detail in checks:
        print(
            theme.status(
                "PASS" if ok else "FAIL",
                f"{name}: {detail}",
                "success" if ok else "danger",
            )
        )
    print(theme.rule())
    return 0 if passed == len(checks) else 1


async def _run_setup_wizard(config_path: Path, *, first_run: bool) -> int:
    """Run the reusable guided setup before the runtime starts."""

    try:
        from corax_console import SetupWizard, probe_openai_compatible
    except ImportError:
        print("corax-console is not installed; cannot run guided setup.")
        return 1

    config = (
        config_mod.load_config(config_path)
        if config_path.exists()
        else config_mod.default_config()
    )
    current = {
        "agent_name": config.agent.name,
        "workspace_path": config.runtime.workspace_path,
        "llm_base_url": config.llm.base_url,
        "llm_model": config.llm.model,
        "memory": config.extensions.bindings.get("memory", "memory.none"),
        "security_mode": config.security.mode,
        "telegram_enabled": (
            "telegram.connector"
            in config.extensions.active.get("channel_connector", [])
        ),
    }
    try:
        result = await SetupWizard().run(
            current,
            first_run=first_run,
            probe=probe_openai_compatible,
        )
    except EOFError:
        print("Setup needs an interactive terminal. Run `corax setup` in a TTY.")
        return 2
    if not result.completed:
        print("Setup cancelled; configuration was not changed.")
        return 2

    values = result.values
    config.agent.name = str(values["agent_name"])
    config.agent.first_run = False
    config.runtime.workspace_path = str(values["workspace_path"])
    config.llm.base_url = str(values["llm_base_url"])
    config.llm.model = str(values["llm_model"])
    config.security.mode = str(values["security_mode"])
    _select_memory(config, str(values["memory"]))
    _set_active_extension(
        config,
        "channel_connector",
        "console.connector",
        True,
    )
    _set_active_extension(
        config,
        "channel_connector",
        "telegram.connector",
        bool(values["telegram_enabled"]),
    )
    config.refresh_legacy_views()
    errors = config_mod.validate_config(config)
    if errors:
        print("Configuration is invalid:")
        for error in errors:
            print(f"  - {error}")
        return 1
    config_mod.save_config(config, config_path)
    ensure_paths(config, config_path)
    print(f"Saved Corax configuration: {config_path}")
    return 0


def _select_memory(config, extension_id: str) -> None:
    spec = config.extensions.available.get(extension_id)
    if spec is None or spec.kind != "memory_provider":
        raise ValueError(f"unknown memory provider: {extension_id}")
    spec.enabled = True
    config.extensions.active["memory_provider"] = [extension_id]
    config.extensions.bindings["memory"] = extension_id


def _set_active_extension(
    config,
    kind: str,
    extension_id: str,
    enabled: bool,
) -> None:
    spec = config.extensions.available.get(extension_id)
    if spec is None or spec.kind != kind:
        raise ValueError(f"unknown {kind}: {extension_id}")
    active = config.extensions.active.setdefault(kind, [])
    spec.enabled = enabled
    if enabled and extension_id not in active:
        active.append(extension_id)
    if not enabled:
        active[:] = [item for item in active if item != extension_id]


def _tool_capability_specs(runtime) -> list[dict]:
    """Describe only kernel-executable tools for the model."""
    from corax.loader.core import _as_pairs

    specs: list[dict] = []
    for cap_id, item in _as_pairs(runtime.tools):
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


async def _run_console_chat(
    app: "CoraxApp",
    *,
    use_tui: bool = False,
) -> int:
    """Run the local interactive console over the same kernel as Telegram."""

    runtime = app.runtime
    if not runtime.core.available:
        print("agent-core is not installed; console chat needs the execution kernel.")
        return 1
    if not runtime.channels.has("console.connector"):
        print("console.connector is not loaded; run `corax setup`.")
        return 1
    model_id = app.config.extensions.bindings.get("primary_model", "llm.local")
    if not runtime.models.has(model_id):
        print(f"{model_id} model provider is not loaded; run `corax setup`.")
        return 1
    try:
        from corax_console import ConsoleChat
    except ImportError:
        print("corax-console is not installed.")
        return 1

    connector = runtime.channels.get("console.connector")
    specs = _tool_capability_specs(runtime)
    system_prompt = _chat_system_prompt(runtime.root_path) or (
        "You are Corax, a helpful local agent. Use tools when action or "
        "verification is required. Reply in the user's language."
    )

    async with runtime.core.session(
        runtime.tools,
        policy=runtime.active_policy(),
        observability=runtime.active_observability(),
    ) as kernel:
        async def run_model(payload, *, session_id):
            return await runtime.invoke_extension(
                model_id,
                payload,
                session_id=session_id,
            )

        async def run_model_stream(payload, *, session_id):
            async for event in runtime.stream_extension(
                model_id,
                payload,
                session_id=session_id,
            ):
                yield event

        async def run_tool(extension_id, payload, *, session_id):
            return await kernel.invoke(
                extension_id,
                payload,
                session_id=session_id,
            )

        async def status_control(_command: str) -> dict:
            status = await runtime.status()
            return {"ok": True, "message": status.render(), **status.to_dict()}

        async def security_control(command: str) -> dict:
            parts = command.split()
            action = parts[0].lower() if parts else "status"
            if action in {"approve", "deny"}:
                if len(parts) != 2:
                    return {
                        "ok": False,
                        "message": f"usage: /security {action} <task-id>",
                    }
                return await kernel.resolve_confirmation(
                    parts[1],
                    approved=action == "approve",
                    actor="local-console",
                )
            return await runtime.security_control(
                command,
                actor="local-console",
                transport="local",
            )

        async def memory_control(command: str) -> dict:
            action, _, argument = command.partition(" ")
            action = action.strip().lower() or "status"
            argument = argument.strip()
            if action == "status":
                loop = runtime.active_memory_loop()
                if loop is None:
                    return {"ok": False, "message": "memory loop is unavailable"}
                result = await runtime.invoke_extension(
                    app.config.extensions.bindings.get(
                        "memory_loop", "memory.loop"
                    ),
                    {"operation": "status"},
                    session_id="console-control",
                )
                return {
                    "ok": True,
                    "message": (
                        f"memory provider: {result.get('provider') or 'none'}; "
                        f"write mode: {result.get('write_mode', 'off')}"
                    ),
                    **result,
                }
            if action == "search" and argument:
                result = await runtime.memory_before_turn(
                    argument,
                    session_id="console-control",
                    scope={"channel": "console"},
                )
                return {
                    "ok": True,
                    "message": result.get("context") or "No matching memories.",
                    **result,
                }
            if action == "remember" and argument:
                result = await runtime.memory_after_turn(
                    argument,
                    "",
                    session_id="console-control",
                    scope={"channel": "console"},
                    explicit=True,
                )
                return {
                    "ok": bool(result.get("stored", False)),
                    "message": (
                        "Memory stored."
                        if result.get("stored")
                        else str(result.get("reason", "Memory was not stored."))
                    ),
                    **result,
                }
            return {
                "ok": False,
                "message": (
                    "usage: /memory status | /memory search <query> | "
                    "/memory remember <text>"
                ),
            }

        state_id = app.config.extensions.bindings.get("state", "")

        async def load_state():
            if not state_id or not runtime.storage.has(state_id):
                return None
            return await runtime.invoke_extension(
                state_id,
                {"operation": "read", "namespace": "console", "key": "local"},
            )

        async def save_state(state):
            if state_id and runtime.storage.has(state_id):
                await runtime.invoke_extension(
                    state_id,
                    {
                        "operation": "write",
                        "namespace": "console",
                        "key": "local",
                        "value": state,
                    },
                )

        chat = ConsoleChat(
            connector=connector,
            run_model=run_model,
            run_model_stream=run_model_stream,
            run_tool=run_tool,
            tools=specs,
            system_prompt=system_prompt,
            model=app.config.llm.model,
            status_command=status_control,
            security_command=security_control,
            memory_command=memory_control,
            memory_before_turn=runtime.memory_before_turn,
            memory_after_turn=runtime.memory_after_turn,
            load_state=load_state,
            save_state=save_state,
            version=CORAX_VERSION,
            workspace=str(runtime.workspace_path),
            security=runtime.security_mode(),
            memory=app.config.extensions.bindings.get("memory", "memory.none"),
            show_banner=app.config.ui.show_banner,
        )
        if use_tui:
            try:
                from corax_tui import run_tui
            except ImportError:
                print(
                    "corax-tui is not installed; falling back to simple chat."
                )
            else:
                return await run_tui(chat)
        return await chat.run()


def _resolve_tool_routing(app: "CoraxApp", selector_available: bool) -> tuple[str, str]:
    """Decide how tools are picked per turn, from ``CORAX_TOOL_ROUTER``.

    Modes: ``llm`` (default) — ask the model which tools to activate;
    ``lexical`` — the keyword/hint top-K selector; ``off`` — offer every tool.
    Falls back gracefully when a mode's prerequisite is missing. Returns the
    resolved mode and a human-readable label for the dashboard.
    """
    mode = (os.getenv("CORAX_TOOL_ROUTER") or "llm").strip().lower()
    if mode == "off":
        return "off", "static full list"
    if mode == "lexical":
        if selector_available:
            return "lexical", "lexical top-K selector"
        return "off", "static full list (no selector)"
    # default: llm router (the chat path guarantees llm.local is loaded)
    return "llm", f"llm router ({app.config.llm.model})"


def _build_tool_routing(
    routing_mode: str, invoke_extension, specs: list[dict], selector, app: "CoraxApp"
) -> dict:
    """Build the gateway's tool-selection kwargs for the resolved routing mode."""
    if routing_mode == "llm":
        from corax.tool_router import LLMToolRouter

        router = LLMToolRouter(
            invoke_extension,
            catalog=specs,
            llm_id=app.config.extensions.bindings.get(
                "primary_model", "llm.local"
            ),
            model=app.config.llm.model,
            fallback=selector.select if selector.available else None,
            log=logging.getLogger("corax.tool_router"),
        )
        return {"tool_router": router.route}
    if routing_mode == "lexical":
        return {"tool_selector": selector.select}
    return {}


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
    model_id = app.config.extensions.bindings.get("primary_model", "llm.local")
    if not runtime.models.has(model_id):
        print(f"{model_id} model provider is not loaded; cannot run the chat.")
        return 1
    if not runtime.channels.has("telegram.connector"):
        print("telegram.connector channel is not loaded; cannot run the chat.")
        return 1

    from corax.gateway import CoraxTelegramGateway
    from corax.tool_discovery import RuntimeToolSelector

    specs = _tool_capability_specs(runtime)
    selector = RuntimeToolSelector(app.config, root_path=runtime.root_path)
    routing_mode, tool_mode_label = _resolve_tool_routing(app, selector.available)
    stream_transport = _telegram_stream_transport()
    system_prompt = _chat_system_prompt(runtime.root_path)
    tool_ids = [
        s["id"]
        for s in specs
        if s["id"]
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
        tool_mode=tool_mode_label,
        stream_transport=stream_transport,
    )

    while True:
        async with runtime.core.session(
            runtime.tools,
            policy=runtime.active_policy(),
            observability=runtime.active_observability(),
        ) as kernel:
            async def invoke_component(
                extension_id: str,
                payload: dict | None = None,
                *,
                session_id: str = "",
            ):
                if runtime.tools.has(extension_id):
                    return await kernel.invoke(
                        extension_id,
                        payload,
                        session_id=session_id or None,
                    )
                return await runtime.invoke_extension(
                    extension_id,
                    payload,
                    session_id=session_id,
                )

            async def control_security(
                command: str,
                *,
                actor: str,
                transport: str,
            ) -> dict:
                parts = command.split()
                action = parts[0].lower() if parts else "status"
                if action in {"approve", "deny"}:
                    authorization = await runtime.security_control(
                        "status",
                        actor=actor,
                        transport=transport,
                    )
                    if not authorization.get("ok"):
                        return authorization
                    if len(parts) != 2:
                        return {
                            "ok": False,
                            "message": (
                                f"usage: /security {action} <task-id>"
                            ),
                            "mode": runtime.security_mode(),
                        }
                    return await kernel.resolve_confirmation(
                        parts[1],
                        approved=action == "approve",
                        actor=actor,
                    )
                return await runtime.security_control(
                    command,
                    actor=actor,
                    transport=transport,
                )

            gateway_kwargs = {
                "run_capability": invoke_component,
                "stream_capability": runtime.stream_extension,
                "capabilities": specs,
                "gateway_available": runtime.services.has("gateway"),
                "telegram_available": runtime.channels.has("telegram.connector"),
                "model": app.config.llm.model,
                "llm_id": model_id,
                "workspace_path": runtime.workspace_path,
                "state_path": runtime.data_path / "telegram-gateway-fallback-state.json",
                "profile_path": runtime.data_path / "profile.md",
                "stream_transport": stream_transport,
                "security_command": control_security,
                "memory_before_turn": runtime.memory_before_turn,
                "memory_after_turn": runtime.memory_after_turn,
            }
            gateway_kwargs.update(
                _build_tool_routing(
                    routing_mode,
                    runtime.invoke_extension,
                    specs,
                    selector,
                    app,
                )
            )
            if system_prompt is not None:
                gateway_kwargs["system_prompt"] = system_prompt
            gateway = CoraxTelegramGateway(**gateway_kwargs)
            print(_theme().status("GATEWAY", "Telegram is running · Ctrl-C to stop", "success"))
            outcome = await _run_gateway_until_stopped(gateway)

        if outcome == "reload":
            print(_theme().status("RELOAD", "reloading agent", "warning"))
            await runtime.reload_config(config_mod.load_config(config_path))
            model_id = runtime.config.extensions.bindings.get(
                "primary_model", "llm.local"
            )
            specs = _tool_capability_specs(runtime)
            selector = RuntimeToolSelector(app.config, root_path=runtime.root_path)
            routing_mode, tool_mode_label = _resolve_tool_routing(app, selector.available)
            stream_transport = _telegram_stream_transport()
            system_prompt = _chat_system_prompt(runtime.root_path)
            tool_ids = [
                s["id"]
                for s in specs
                if s["id"]
            ]
            _print_chat_dashboard(
                app,
                specs,
                tool_ids,
                tool_mode=tool_mode_label,
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
            print(_theme().status("GATEWAY", "stopping Telegram", "warning"))
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
    tool_mode: str = "static full list",
    stream_transport: str = "edit",
) -> None:
    runtime = app.runtime
    executable = runtime.core.executable_ids(runtime.tools)
    has_gateway = runtime.services.has("gateway")
    has_telegram = runtime.channels.has("telegram.connector")
    allowed_chats = app.config.telegram.allowed_chats.strip() or "not set"
    rows = [
        ("mode", "telegram chat gateway"),
        ("model", app.config.llm.model),
        ("kernel", f"ready, {len(executable)} executable capability(ies)"),
        ("gateway", "runtime service" if has_gateway else "fallback local state"),
        ("connector", "telegram.connector" if has_telegram else "missing"),
        ("streaming", f"{stream_transport} transport"),
        ("tool mode", tool_mode),
        ("tools", ", ".join(tool_ids) or "none"),
        ("allowed chats", allowed_chats),
        ("workspace", str(runtime.workspace_path)),
    ]

    theme = _theme()
    print(theme.header("Corax Chat Gateway"))
    for label, value in rows:
        role = "success" if label in {"kernel", "gateway"} else "text"
        print(theme.field(label, value, role))
    print(theme.rule())


def _print_setup_overview(app: "CoraxApp") -> None:
    runtime = app.runtime
    theme = _theme()
    print(theme.header("Corax Setup"))
    rows = [
        ("config", str(app.config_path)),
        ("profile", app.config.agent.profile),
        ("model", app.config.llm.model),
        ("telegram", "configured" if os.getenv("CORAX_TELEGRAM_BOT_TOKEN") else "token missing"),
        ("web search", app.config.websearch.base_url),
        ("workspace", str(runtime.workspace_path)),
        ("next", "corax"),
    ]
    for label, value in rows:
        role = "warning" if value == "token missing" else "text"
        print(theme.field(label, value, role))
    print(theme.rule())


def _print_warning(title: str, message: str) -> None:
    print("\n" + _theme().status(title, message, "warning"))


def _theme() -> TerminalTheme:
    return TerminalTheme.detect(sys.stdout)


def _print_result(title: str, value: object) -> None:
    theme = _theme()
    print(theme.header(title))
    print(safe_text(value))
    print(theme.rule())


def _do_init(config_path: Path) -> int:
    existed = config_path.exists()
    config = config_mod.load_config(config_path) if existed else config_mod.create_default_config(config_path)
    paths = ensure_paths(config, config_path)
    theme = _theme()
    if config.ui.show_banner:
        print(theme.logo())
    print()
    if existed:
        print(theme.status("CONFIG", f"present: {config_path}", "accent"))
    else:
        print(theme.status("CONFIG", f"created: {config_path}", "success"))
    print(theme.field("workspace", paths.workspace))
    print(theme.field("data", paths.data))
    print(theme.field("logs", paths.logs))
    print()
    errors = config_mod.validate_config(config)
    if errors:
        print(theme.status("CONFIG", "validation warnings", "warning"))
        for err in errors:
            print(f"  - {err}")
        return 1
    print(theme.status("READY", "configuration is valid", "success"))
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
