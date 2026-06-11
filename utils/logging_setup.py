"""Structured logging: console output plus a rotating log file."""

import logging
import os
from logging.handlers import RotatingFileHandler

import config

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging() -> None:
    """Configure the root logger. Call once, before the bot starts."""
    os.makedirs(os.path.dirname(config.LOG_FILE) or ".", exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(config.LOG_LEVEL)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        config.LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # discord.py is very chatty below INFO; raise its floor so our own
    # DEBUG logging stays readable when LOG_LEVEL=DEBUG.
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
