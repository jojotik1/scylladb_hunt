"""
utils/logging.py
----------------
Centralised logging configuration for the GTM Hunter.
Import make_logger from here — never configure logging in individual modules.
"""

import logging
import sys


def configure_logging(level: int = logging.INFO) -> None:
    """Call once at startup (in main.py). Idempotent."""
    logging.basicConfig(
        format="[%(asctime)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        level=level,
        stream=sys.stdout,
    )


def make_logger(name: str) -> logging.Logger:
    """Return a named logger. Works before or after configure_logging()."""
    return logging.getLogger(name)
