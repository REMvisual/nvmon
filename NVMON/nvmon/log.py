"""Logging: file + console output."""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from . import config

_logger: logging.Logger | None = None


def setup(verbose: bool = False, no_color: bool = False) -> logging.Logger:
    """Configure logging to console + file."""
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger("nvmon")
    logger.setLevel(logging.DEBUG)

    # File handler — one log per day
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    fh = logging.FileHandler(config.LOGS_DIR / f"nvmon-{today}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)

    if no_color or not sys.stdout.isatty():
        ch.setFormatter(logging.Formatter("%(message)s"))
    else:
        ch.setFormatter(_ColorFormatter())

    logger.addHandler(ch)
    _logger = logger
    return logger


def get() -> logging.Logger:
    """Get the configured logger, or set up a default one."""
    global _logger
    if _logger is None:
        return setup()
    return _logger


class _ColorFormatter(logging.Formatter):
    """Simple ANSI color formatter for console output."""

    COLORS = {
        logging.DEBUG: "\033[90m",     # gray
        logging.INFO: "\033[0m",       # default
        logging.WARNING: "\033[33m",   # yellow
        logging.ERROR: "\033[31m",     # red
        logging.CRITICAL: "\033[91m",  # bright red
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, self.RESET)
        msg = record.getMessage()
        return f"{color}{msg}{self.RESET}"
