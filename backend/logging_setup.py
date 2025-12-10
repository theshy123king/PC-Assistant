from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
BACKEND_LOG = LOG_DIR / "backend.log"
BACKEND_EVENTS_LOG = LOG_DIR / "backend_events.log"


def _safe_mkdir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Ignore failures; logging will fall back to stderr.
        pass


def setup_logging() -> None:
    """Configure rotating file logging for the backend."""
    _safe_mkdir(LOG_DIR)
    handlers = []
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            BACKEND_LOG,
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setLevel(logging.INFO)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except Exception:
        # If file logging fails, continue with stderr-only to avoid crashes.
        pass

    if not handlers:
        # Nothing to do if we cannot create any handler.
        return

    logging.basicConfig(
        level=logging.INFO,
        handlers=handlers,
    )

    # Dedicated structured event logger (human-readable JSON lines).
    try:
        event_handler = logging.handlers.RotatingFileHandler(
            BACKEND_EVENTS_LOG,
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
            delay=True,
        )
        event_handler.setLevel(logging.INFO)
        event_formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        event_handler.setFormatter(event_formatter)
        event_logger = logging.getLogger("backend.events")
        event_logger.setLevel(logging.INFO)
        event_logger.addHandler(event_handler)
        event_logger.propagate = False
    except Exception:
        # Do not fail app startup if structured logging cannot be created.
        pass

    # Align uvicorn loggers to use same handlers/format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        for h in handlers:
            logger.addHandler(h)
        logger.propagate = False
