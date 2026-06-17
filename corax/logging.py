"""Logging configuration.

A single :func:`setup_logging` call wires a console handler and a rolling
file handler at ``<logs>/corax.log``. It is idempotent: calling it again
(e.g. after a config reload) replaces the handlers instead of stacking
them.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(corax_component)s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_CONSOLE_DATE_FORMAT = "%H:%M:%S"
_LOGGER_NAME = "corax"

_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}


class _CoraxConsoleFormatter(logging.Formatter):
    """Compact color formatter for human terminal logs."""

    def __init__(self, *, color: bool) -> None:
        super().__init__(_CONSOLE_FORMAT, datefmt=_CONSOLE_DATE_FORMAT)
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        record.corax_component = self._component(record.name)
        line = super().format(record)
        if not self.color:
            return line
        color = _COLORS.get(record.levelname, "")
        timestamp = self.formatTime(record, self.datefmt)
        line = line.replace(timestamp, f"{_DIM}{timestamp}{_RESET}", 1)
        line = line.replace(record.levelname.ljust(7), f"{color}{record.levelname:<7}{_RESET}", 1)
        line = line.replace(record.corax_component, f"{_BOLD}{record.corax_component}{_RESET}", 1)
        return line

    @staticmethod
    def _component(name: str) -> str:
        if name == _LOGGER_NAME:
            return "main"
        prefix = f"{_LOGGER_NAME}."
        if name.startswith(prefix):
            return name[len(prefix):]
        return name


def setup_logging(level: str = "INFO", logs_path: Path | None = None) -> logging.Logger:
    """Configure and return the ``corax`` logger."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(_resolve_level(level))
    logger.propagate = False

    # Replace existing handlers so repeated calls stay idempotent.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler()
    console.setFormatter(_CoraxConsoleFormatter(color=_use_color(console.stream)))
    logger.addHandler(console)

    if logs_path is not None:
        logs_dir = Path(logs_path)
        logs_dir.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
        file_handler = logging.FileHandler(logs_dir / "corax.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the ``corax`` logger."""
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)


def _resolve_level(level: str) -> int:
    return getattr(logging, str(level).upper(), logging.INFO)


def _use_color(stream: object) -> bool:
    mode = os.getenv("CORAX_COLOR", "auto").strip().lower()
    if mode in {"1", "true", "yes", "always", "on"}:
        return True
    if mode in {"0", "false", "no", "never", "off"}:
        return False
    return bool(getattr(stream, "isatty", lambda: False)()) and sys.platform != "win32"
