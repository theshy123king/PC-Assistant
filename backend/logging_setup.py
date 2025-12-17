from __future__ import annotations

import logging
import logging.handlers
import os
import sys
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


def _flag_from_env(var: str, default: bool) -> bool:
    value = os.getenv(var)
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "off", "no", "none"}


class _TeeStream:
    def __init__(self, stream: object, logger: logging.Logger, level: int) -> None:
        self._stream = stream
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, message: object) -> int:
        if message is None:
            return 0
        if isinstance(message, bytes):
            text = message.decode(errors="replace")
        else:
            text = str(message)
        if text:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                line = line.rstrip("\r")
                if line.strip():
                    self._logger.log(self._level, line)
        if hasattr(self._stream, "write"):
            try:
                return int(self._stream.write(text))
            except Exception:
                return len(text)
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._logger.log(self._level, self._buffer.rstrip("\r"))
        self._buffer = ""
        if hasattr(self._stream, "flush"):
            try:
                self._stream.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        if hasattr(self._stream, "isatty"):
            try:
                return bool(self._stream.isatty())
            except Exception:
                return False
        return False

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


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

    if _flag_from_env("BACKEND_CAPTURE_STDIO", True):
        stdout_logger = logging.getLogger("backend.stdout")
        stderr_logger = logging.getLogger("backend.stderr")
        stdout_logger.setLevel(logging.INFO)
        stderr_logger.setLevel(logging.ERROR)
        sys.stdout = _TeeStream(sys.stdout, stdout_logger, logging.INFO)
        sys.stderr = _TeeStream(sys.stderr, stderr_logger, logging.ERROR)
