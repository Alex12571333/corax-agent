#!/usr/bin/env python3
"""Corax Agent — collision-free CLI entrypoint.

Usage:
    corax                          # first-run setup, then inline TUI
    corax tui                      # inline terminal chat
    corax chat                     # simple line-oriented console fallback
    corax setup                    # guided setup wizard
    corax settings                 # advanced settings menu
    corax gateway                  # run the Telegram gateway
    corax status                   # print runtime status and exit
    corax security status          # show the active permission mode
    corax security mode auto       # switch ask / auto / full
    corax prompts status           # inspect layered prompt composition
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

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on Windows
    resource = None  # type: ignore[assignment]

from corax_ui import TerminalTheme, safe_text

from corax import __version__ as CORAX_VERSION
from corax import config as config_mod
from corax.app import CoraxApp
from corax.paths import default_config_path, ensure_paths
from corax.tool_router import TOOL_CALL_ID, TOOL_SEARCH_ID


def _minimal_runtime_snapshot(app: CoraxApp) -> dict[str, object]:
    try:
        rss: int | None = (
            int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            if resource is not None
            else None
        )
        if sys.platform != "darwin":
            rss = rss * 1024 if rss is not None else None
    except (OSError, ValueError):
        rss = None
    process = {"pid": os.getpid(), "rss_bytes": rss}
    runtime = app.runtime
    if runtime is None:
        return {**process, "running": False}
    try:
        status = runtime.snapshot()
    except Exception as exc:  # noqa: BLE001 - signal handling must still exit
        return {
            **process,
            "running": bool(runtime.running),
            "snapshot_error": type(exc).__name__,
        }
    snapshot: dict[str, object] = {
        **process,
        "running": status.running,
        "uptime_seconds": round(status.uptime_seconds, 3),
        "mode": status.mode,
    }
    diagnostic = getattr(app, "_chat_diagnostic_snapshot", None)
    if callable(diagnostic):
        try:
            snapshot["chat"] = diagnostic()
        except Exception as exc:  # noqa: BLE001 - signal logging must still exit
            snapshot["chat_error"] = type(exc).__name__
    return snapshot


def _log_shutdown_signal(app: CoraxApp, signum: int) -> None:
    logger = app.log or logging.getLogger("corax")
    logger.warning(
        "shutdown requested: signal=%s runtime=%s",
        signal.Signals(signum).name,
        _minimal_runtime_snapshot(app),
    )


def _request_shutdown(
    app: CoraxApp,
    task: Any,
    state: dict[str, int],
    signum: int,
) -> None:
    if not state["signum"]:
        state["signum"] = int(signum)
        _log_shutdown_signal(app, signum)
    task.cancel()


def _install_shutdown_signal_handlers(
    app: CoraxApp,
) -> tuple[dict[str, int], dict[int, Any]]:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    state = {"signum": 0}
    previous: dict[int, Any] = {}
    if task is None:
        return state, previous
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            old_handler = signal.getsignal(signum)
            loop.add_signal_handler(
                signum,
                _request_shutdown,
                app,
                task,
                state,
                signum,
            )
        except (NotImplementedError, OSError, RuntimeError, ValueError):
            continue
        previous[signum] = old_handler
    return state, previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    loop = asyncio.get_running_loop()
    for signum, handler in previous.items():
        try:
            loop.remove_signal_handler(signum)
            signal.signal(signum, handler)
        except (NotImplementedError, OSError, RuntimeError, ValueError):
            pass


def _parse_confirmation_command(
    command: str,
) -> tuple[str, str] | tuple[None, str]:
    """Return ``(task_id, resolution)`` or ``(None, usage/error)``."""

    parts = command.split()
    action = parts[0].lower() if parts else ""
    if action not in {"approve", "deny"}:
        return None, ""
    if len(parts) not in {2, 3}:
        return (
            None,
            (
                "usage: /security approve <task-id> once|turn|session"
                if action == "approve"
                else "usage: /security deny <task-id> once|rule"
            ),
        )
    scope = parts[2].lower() if len(parts) == 3 else "once"
    resolutions = {
        "approve": {
            "once": "allow_once",
            "turn": "allow_turn",
            "session": "allow_session",
        },
        "deny": {
            "once": "deny_once",
            "rule": "deny_rule",
        },
    }
    resolution = resolutions[action].get(scope)
    if resolution is None:
        choices = "|".join(resolutions[action])
        return None, f"usage: /security {action} <task-id> {choices}"
    return parts[1], resolution


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
            "prompts",
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
        help="command to run (default: inline terminal chat)",
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
        if setup_result is None and command in {
            "setup",
            "tui",
            "chat",
            "gateway",
        }:
            print("Opening the built-in settings menu instead.")
            command = "settings"
        elif setup_result is None:
            return 1
        elif setup_result != 0:
            return setup_result
        elif command == "setup":
            return 0

    app = CoraxApp(config_path)
    shutdown_state = {"signum": 0}
    previous_signal_handlers: dict[int, Any] = {}
    try:
        await app.boot()
        shutdown_state, previous_signal_handlers = (
            _install_shutdown_signal_handlers(app)
        )
        if command == "status":
            status = await app.runtime.status()
            _print_result("Runtime status", status.render())
        elif command == "security":
            security_command = " ".join(args.command_args) or "status"
            result = await app.runtime.security_control(security_command)
            _print_result("Security", result.get("message") or result)
            return 0 if result.get("ok") or result.get("challenge") else 2
        elif command == "prompts":
            service_id = app.config.extensions.bindings.get(
                "prompts", "prompts.runtime"
            )
            if not app.runtime.services.has(service_id):
                print("prompts.runtime is not loaded.")
                return 1
            if args.command_args and args.command_args[0] == "identity":
                identity_args = args.command_args[1:]
                usage = (
                    "usage: corax prompts identity "
                    "<status|show|replace|reset|onboarding> "
                    "<profile|memory> [file]"
                )
                if len(identity_args) < 2:
                    print(usage)
                    return 2
                action, target = identity_args[:2]
                if action not in {
                    "status",
                    "show",
                    "replace",
                    "reset",
                    "onboarding",
                } or target not in {"profile", "memory"}:
                    print(usage)
                    return 2
                payload = {
                    "operation": "identity",
                    "action": action,
                    "target": target,
                }
                if action == "replace":
                    if len(identity_args) != 3:
                        print(usage)
                        return 2
                    try:
                        payload["content"] = (
                            sys.stdin.read()
                            if identity_args[2] == "-"
                            else Path(identity_args[2])
                            .expanduser()
                            .read_text(encoding="utf-8")
                        )
                    except (OSError, UnicodeError) as exc:
                        print(f"cannot read identity replacement: {exc}")
                        return 2
                elif len(identity_args) != 2:
                    print(usage)
                    return 2
                result = await app.runtime.invoke_extension(
                    service_id,
                    payload,
                    session_id="prompts-control",
                )
                if action == "show":
                    print(str(result.get("content") or ""))
                else:
                    _print_result("Prompt identity", result)
                return 0
            requested = args.command_args[0] if args.command_args else "status"
            operation = (
                requested
                if requested in {"status", "reload", "validate", "migrate"}
                else "status"
            )
            result = await app.runtime.invoke_extension(
                service_id,
                {"operation": operation},
                session_id="prompts-control",
            )
            _print_result("Prompts", result)
            return 0 if result.get("ok", True) else 1
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
            agent_root = Path(__file__).resolve().parents[1]
            report = await asyncio.to_thread(
                run_evaluations,
                agent_root,
                agent_root.parent,
            )
            _print_result("Evaluations", report.render())
            return 0 if report.ok else 1
        elif command == "doctor":
            return await _run_doctor(app)
        elif command == "gateway":
            return await _run_chat(app, config_path)
        elif command in {"tui", "chat"}:
            if (
                not app.runtime.channels.has("console.connector")
                or not app.runtime.active_generation_model_id()
            ):
                print(
                    "Console chat is unavailable; opening the built-in "
                    "settings menu instead."
                )
                await app.run_menu()
            else:
                return await _run_console_chat(
                    app,
                    use_tui=command == "tui",
                )
        else:
            _print_setup_overview(app)
            await app.run_menu()
    except asyncio.CancelledError:
        signum = shutdown_state["signum"]
        if signum:
            return 128 + signum
        raise
    finally:
        _restore_signal_handlers(previous_signal_handlers)
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
    model_id = runtime.active_generation_model_id()
    sandbox = runtime.active_sandbox_executor()
    sandbox_status = (
        sandbox.status()
        if sandbox is not None and callable(getattr(sandbox, "status", None))
        else {}
    )
    object_requested = app.config.runtime.execution_mode == "object"
    object_ready = runtime.object_execution_available("console")
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
            sandbox is not None,
            app.config.extensions.bindings.get("sandbox", "not configured"),
        ),
        (
            "object execution",
            not object_requested or object_ready,
            (
                f"ready via {sandbox_status.get('python_backend', 'unknown')}"
                if object_ready
                else (
                    "legacy selected"
                    if not object_requested
                    else "inactive: start Docker and preload the configured image"
                )
            ),
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


async def _run_setup_wizard(
    config_path: Path,
    *,
    first_run: bool,
) -> int | None:
    """Run the reusable guided setup before the runtime starts."""

    try:
        from corax_console import SetupWizard, probe_openai_compatible
    except ImportError:
        print("corax-console is not installed; guided setup is unavailable.")
        return None

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
    runtime.sync_tool_catalog()
    return runtime.tool_routing.all_specs()


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
    model_id = runtime.active_generation_model_id()
    if not model_id:
        print("No generation model provider is loaded; run `corax setup`.")
        return 1
    try:
        from corax_console import ConsoleChat, discover_model_context_window
    except ImportError:
        print("corax-console is not installed.")
        return 1

    connector = runtime.channels.get("console.connector")
    local_channel = "tui" if use_tui else "console"
    specs = _tool_capability_specs(runtime)
    system_prompt = _chat_system_prompt(runtime.root_path) or (
        "You are Corax, a helpful local agent. Use tools when action or "
        "verification is required. Reply in the user's language."
    )

    async with runtime.core.session(
        runtime.tools,
        policy=runtime.active_policy(),
        observability=runtime.active_observability(),
        result_transform=runtime.compact_tool_result,
    ) as kernel:
        chat: Any | None = None

        async def routed_payload(payload, *, session_id):
            turn_id = str(getattr(chat, "turn_id", "") or "")
            if not turn_id:
                raise RuntimeError("console model call has no active turn")
            return await runtime.prepare_tool_model_request(
                payload,
                session_id=session_id,
                turn_id=turn_id,
                channel=local_channel,
                kernel=kernel,
            )

        async def run_model(payload, *, session_id):
            return await runtime.invoke_extension(
                model_id,
                await routed_payload(payload, session_id=session_id),
                session_id=session_id,
            )

        async def run_model_stream(payload, *, session_id):
            async for event in runtime.stream_extension(
                model_id,
                await routed_payload(payload, session_id=session_id),
                session_id=session_id,
            ):
                yield event

        async def run_tool(extension_id, payload, *, session_id):
            policy_metadata = {
                "actor": "local-console",
                "transport": "local",
                "workspace": str(runtime.workspace_path),
                "environment": "local",
            }
            turn_id = getattr(chat, "turn_id", "")
            if isinstance(turn_id, str) and turn_id:
                policy_metadata["turn_id"] = turn_id
            if not isinstance(turn_id, str) or not turn_id:
                raise PermissionError("tool call has no active console turn")
            return await runtime.invoke_turn_tool(
                extension_id,
                payload,
                kernel=kernel,
                session_id=session_id,
                turn_id=turn_id,
                channel=local_channel,
                policy_metadata=policy_metadata,
                event_sink=getattr(chat, "emit_runtime_event", None),
            )

        async def status_control(_command: str) -> dict:
            status = await runtime.status()
            return {"ok": True, "message": status.render(), **status.to_dict()}

        async def security_control(command: str) -> dict:
            parts = command.split()
            action = parts[0].lower() if parts else "status"
            if action in {"approve", "deny"}:
                task_id, resolution = _parse_confirmation_command(command)
                if task_id is None:
                    return {
                        "ok": False,
                        "message": resolution,
                    }
                resolved = await runtime.resolve_confirmation(
                    kernel,
                    task_id,
                    resolution,
                    actor="local-console",
                    transport="local",
                )
                return await runtime.resume_object_confirmation(
                    task_id,
                    resolved,
                    kernel=kernel,
                    event_sink=getattr(chat, "emit_runtime_event", None),
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
                    loop.id,
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

        async def memory_after_turn(*args, session_id, **kwargs):
            scope = dict(kwargs.get("scope") or {})
            scope["channel"] = local_channel
            kwargs["scope"] = scope
            try:
                return await runtime.memory_after_turn(
                    *args,
                    session_id=session_id,
                    **kwargs,
                )
            finally:
                runtime.tool_routing.end_turn(
                    session_id=session_id,
                    channel=local_channel,
                )

        async def abort_turn(*, session_id, scope):
            await runtime.abort_turn(
                session_id=session_id,
                channel=local_channel,
                turn_id=str((scope or {}).get("turn_id") or ""),
            )

        host_prompt_assembly = runtime.active_prompt_runtime() is not None
        context_window = await discover_model_context_window(
            app.config.llm.base_url,
            app.config.llm.model,
        )
        runtime.set_model_context_window(context_window)
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
            memory_before_turn=(
                None if host_prompt_assembly else runtime.memory_before_turn
            ),
            memory_after_turn=memory_after_turn,
            abort_turn=abort_turn,
            host_prompt_assembly=host_prompt_assembly,
            load_state=load_state,
            save_state=save_state,
            version=os.environ.get("CORAX_RELEASE_VERSION", CORAX_VERSION),
            workspace=str(runtime.workspace_path),
            security=runtime.security_mode(),
            memory=app.config.extensions.bindings.get("memory", "memory.none"),
            show_banner=app.config.ui.show_banner,
            max_history_messages=None,
        )
        app._chat_diagnostic_snapshot = chat.diagnostic_snapshot
        if use_tui:
            try:
                from corax_tui import run_tui
            except ImportError:
                print(
                    "corax-tui is not installed; falling back to simple chat."
                )
            else:
                return await run_tui(chat, context_window=context_window)
        return await chat.run()


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
    model_id = runtime.active_generation_model_id()
    if not model_id:
        print("No generation model provider is loaded; cannot run the chat.")
        return 1
    if not runtime.channels.has("telegram.connector"):
        print("telegram.connector channel is not loaded; cannot run the chat.")
        return 1

    from corax.gateway import CoraxTelegramGateway
    from corax_console import discover_model_context_window

    runtime.set_model_context_window(
        await discover_model_context_window(
            app.config.llm.base_url,
            app.config.llm.model,
        )
    )

    specs = _tool_capability_specs(runtime)
    tool_mode_label = (
        f"embedding top-{app.config.tool_routing.top_k} "
        f"({app.config.tool_routing.model})"
    )
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
            result_transform=runtime.compact_tool_result,
        ) as kernel:
            async def invoke_component(
                extension_id: str,
                payload: dict | None = None,
                *,
                session_id: str = "",
                policy_metadata: dict[str, str] | None = None,
            ):
                data = dict(payload or {})
                if extension_id == model_id:
                    turn_id = str(data.get("_corax_turn_id", "") or "")
                    if not turn_id:
                        raise RuntimeError("Telegram model call has no active turn")
                    data = await runtime.prepare_tool_model_request(
                        data,
                        session_id=session_id,
                        turn_id=turn_id,
                        channel="telegram",
                        kernel=kernel,
                    )
                    return await runtime.invoke_extension(
                        extension_id,
                        data,
                        session_id=session_id,
                    )
                if extension_id in {TOOL_SEARCH_ID, TOOL_CALL_ID} or runtime.tools.has(
                    extension_id
                ):
                    turn_id = str((policy_metadata or {}).get("turn_id", "") or "")
                    return await runtime.invoke_turn_tool(
                        extension_id,
                        data,
                        kernel=kernel,
                        session_id=session_id,
                        turn_id=turn_id,
                        channel="telegram",
                        policy_metadata=policy_metadata,
                    )
                return await runtime.invoke_extension(
                    extension_id,
                    data,
                    session_id=session_id,
                )

            async def stream_component(
                extension_id: str,
                payload: dict | None = None,
                *,
                session_id: str = "",
            ):
                data = dict(payload or {})
                if extension_id == model_id:
                    turn_id = str(data.get("_corax_turn_id", "") or "")
                    if not turn_id:
                        raise RuntimeError("Telegram model call has no active turn")
                    data = await runtime.prepare_tool_model_request(
                        data,
                        session_id=session_id,
                        turn_id=turn_id,
                        channel="telegram",
                        kernel=kernel,
                    )
                async for event in runtime.stream_extension(
                    extension_id,
                    data,
                    session_id=session_id,
                ):
                    yield event

            async def memory_after_turn(*args, session_id, **kwargs):
                try:
                    return await runtime.memory_after_turn(
                        *args,
                        session_id=session_id,
                        **kwargs,
                    )
                finally:
                    runtime.tool_routing.end_turn(
                        session_id=session_id,
                        channel="telegram",
                    )

            async def abort_turn(*, session_id, scope):
                await runtime.abort_turn(
                    session_id=session_id,
                    channel="telegram",
                    turn_id=str((scope or {}).get("turn_id") or ""),
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
                    task_id, resolution = _parse_confirmation_command(command)
                    if task_id is None:
                        return {
                            "ok": False,
                            "message": resolution,
                            "mode": runtime.security_mode(),
                        }
                    return await runtime.resolve_confirmation(
                        kernel,
                        task_id,
                        resolution,
                        actor=actor,
                        transport=transport,
                    )
                return await runtime.security_control(
                    command,
                    actor=actor,
                    transport=transport,
                )

            host_prompt_assembly = runtime.active_prompt_runtime() is not None
            gateway_kwargs = {
                "run_capability": invoke_component,
                "stream_capability": stream_component,
                "capabilities": specs,
                "gateway_available": runtime.services.has("gateway"),
                "telegram_available": runtime.channels.has("telegram.connector"),
                "model": app.config.llm.model,
                "llm_id": model_id,
                "workspace_path": runtime.workspace_path,
                "state_path": runtime.data_path / "telegram-gateway-fallback-state.json",
                "stream_transport": stream_transport,
                "security_command": control_security,
                "memory_before_turn": (
                    None if host_prompt_assembly else runtime.memory_before_turn
                ),
                "memory_after_turn": memory_after_turn,
                "abort_turn": abort_turn,
                "host_prompt_assembly": host_prompt_assembly,
                "profile_path": (
                    None
                    if host_prompt_assembly
                    else runtime.data_path / "profile.md"
                ),
                "tool_mode": tool_mode_label,
            }
            if system_prompt is not None:
                gateway_kwargs["system_prompt"] = system_prompt
            gateway = CoraxTelegramGateway(**gateway_kwargs)
            print(_theme().status("GATEWAY", "Telegram is running · Ctrl-C to stop", "success"))
            outcome = await _run_gateway_until_stopped(gateway)

        if outcome == "reload":
            print(_theme().status("RELOAD", "reloading agent", "warning"))
            await app.reload_config()
            model_id = runtime.active_generation_model_id()
            if not model_id:
                print("No generation model provider is loaded after reload.")
                return 1
            if not runtime.channels.has("telegram.connector"):
                print("telegram.connector is not loaded after reload.")
                return 1
            specs = _tool_capability_specs(runtime)
            tool_mode_label = (
                f"embedding top-{app.config.tool_routing.top_k} "
                f"({app.config.tool_routing.model})"
            )
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
