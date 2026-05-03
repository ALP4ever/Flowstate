"""Centralized rotating file logger for FlowState.

Each project root gets its own logger writing to ``.flowstate/logs/flowstate.log``.
The logger is reused across calls to avoid attaching duplicate handlers.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILENAME = "flowstate.log"
LOG_DIR_NAME = "logs"
MAX_BYTES = 512 * 1024
BACKUP_COUNT = 3

_loggers: dict[str, logging.Logger] = {}


def get_logger(project_root: Path | str) -> logging.Logger:
    """Return a rotating file logger scoped to ``project_root``."""

    root = Path(project_root).resolve()
    key = str(root)
    if key in _loggers:
        return _loggers[key]

    log_dir = root / ".flowstate" / LOG_DIR_NAME
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fall back to a no-op logger if we can't create the log directory.
        logger = logging.getLogger(f"flowstate.disabled.{key}")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        _loggers[key] = logger
        return logger

    logger = logging.getLogger(f"flowstate.{key}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = RotatingFileHandler(
            log_dir / LOG_FILENAME,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)

    _loggers[key] = logger
    return logger
