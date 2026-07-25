"""Logging configuration.

A single :func:`setup_logging` call wires a console handler and a rolling
file handler at ``<logs>/corax.log``. It is idempotent: calling it again
(e.g. after a config reload) replaces the handlers instead of stacking
them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from corax_ui import TerminalTheme

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(corax_component)s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_CONSOLE_DATE_FORMAT = "%H:%M:%S"
_LOGGER_NAME = "corax"

_ROLES = {
    "DEBUG": "accent",
    "INFO": "success",
    "WARNING": "warning",
    "ERROR": "danger",
    "CRITICAL": "danger",
}


class _CoraxConsoleFormatter(logging.Formatter):
    """Compact color formatter for human terminal logs."""

    def __init__(self, *, color: bool) -> None:
        super().__init__(_CONSOLE_FORMAT, datefmt=_CONSOLE_DATE_FORMAT)
        self.theme = TerminalTheme(color)

    def format(self, record: logging.LogRecord) -> str:
        record.corax_component = self._component(record.name)
        line = super().format(record)
        if not self.theme.enabled:
            return line
        role = _ROLES.get(record.levelname, "text")
        timestamp = self.formatTime(record, self.datefmt)
        line = line.replace(timestamp, self.theme.paint(timestamp, "muted", dim=True), 1)
        line = line.replace(
            record.levelname.ljust(7),
            self.theme.paint(f"{record.levelname:<7}", role, bold=True),
            1,
        )
        line = line.replace(
            record.corax_component,
            self.theme.paint(record.corax_component, "text", bold=True),
            1,
        )
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
    return TerminalTheme.detect(stream).enabled
