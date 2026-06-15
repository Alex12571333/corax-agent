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
        else:
            await app.run_menu()
    finally:
        await app.shutdown()
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
