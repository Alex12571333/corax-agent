"""Logging configuration.

A single :func:`setup_logging` call wires a console handler and a rolling
file handler at ``<logs>/corax.log``. It is idempotent: calling it again
(e.g. after a config reload) replaces the handlers instead of stacking
them.
"""

from __future__ import annotations

import logging
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_LOGGER_NAME = "corax"


def setup_logging(level: str = "INFO", logs_path: Path | None = None) -> logging.Logger:
    """Configure and return the ``corax`` logger."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(_resolve_level(level))
    logger.propagate = False

    # Replace existing handlers so repeated calls stay idempotent.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    if logs_path is not None:
        logs_dir = Path(logs_path)
        logs_dir.mkdir(parents=True, exist_ok=True)
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
