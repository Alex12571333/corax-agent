"""Screen renderers.

Pure functions that turn config / status into a list of display lines.
Keeping them free of I/O makes them trivial to unit-test and keeps
:mod:`corax_agent.menu` focused on flow control.
"""

from __future__ import annotations

from ..config import AgentConfig, ProviderSpec
from ..paths import AgentPaths
from ..runtime import RuntimeStatus

MAIN_MENU = [
    "",
    "Corax Agent",
    "",
    "  1. Status",
    "  2. Runtime Settings",
    "  3. Planner",
    "  4. Memory",
    "  5. Connectors",
    "  6. Capabilities",
    "  7. Security",
    "  8. Limits",
    "  9. Paths",
    " 10. Save and Exit",
    "  0. Exit without saving",
    "",
]


def _mark(flag: bool) -> str:
    return "[x]" if flag else "[ ]"


def status_screen(status: RuntimeStatus) -> list[str]:
    return ["Runtime Status", "", status.render()]


def providers_screen(
    title: str,
    providers: dict[str, ProviderSpec],
    active: list[str],
) -> list[str]:
    out = [title, ""]
    if not providers:
        out.append("  (no providers registered)")
        return out
    for pid, spec in providers.items():
        active_mark = "*" if pid in active else " "
        out.append(f"  {active_mark} {_mark(spec.enabled)} {pid}  ({spec.type})")
        if spec.description:
            out.append(f"        {spec.description}")
    out.append("")
    out.append("  legend: '*' = active/enabled-in-use, [x] = provider enabled")
    return out


def runtime_screen(config: AgentConfig) -> list[str]:
    rt = config.runtime
    return [
        "Runtime Settings",
        "",
        f"  1. log_level      : {rt.log_level}",
        f"  2. autostart      : {rt.autostart}",
        f"  3. workspace_path : {rt.workspace_path}",
        f"  4. data_path      : {rt.data_path}",
        f"  5. logs_path      : {rt.logs_path}",
    ]


def security_screen(config: AgentConfig) -> list[str]:
    sec = config.security
    out = [
        "Security",
        "",
        f"  1. mode             : {sec.mode}",
        f"  2. core_readonly    : {sec.core_readonly}",
        f"  3. allow_shell      : {sec.allow_shell}",
        f"  4. allow_file_write : {sec.allow_file_write}",
        "",
        "  blocked_paths:",
    ]
    for path in sec.blocked_paths:
        out.append(f"    - {path}")
    return out


def limits_screen(config: AgentConfig) -> list[str]:
    lim = config.limits
    return [
        "Limits",
        "",
        f"  1. max_parallel_tasks         : {lim.max_parallel_tasks}",
        f"  2. max_plan_tasks             : {lim.max_plan_tasks}",
        f"  3. max_tasks_per_correlation  : {lim.max_tasks_per_correlation}",
        f"  4. task_timeout_seconds       : {lim.task_timeout_seconds}",
        f"  5. max_payload_mb             : {lim.max_payload_mb}",
    ]


def paths_screen(paths: AgentPaths) -> list[str]:
    out = ["Resolved Paths", ""]
    for key, value in paths.as_dict().items():
        out.append(f"  {key:<10}: {value}")
    return out
