from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, Optional

# Runtime context pointers
CURRENT_CONTEXT: ContextVar[Any] = ContextVar("CURRENT_CONTEXT", default=None)
ACTIVE_WINDOW: ContextVar[Optional[Dict[str, Any]]] = ContextVar("ACTIVE_WINDOW", default=None)


def set_current_context(ctx: Any):
    return CURRENT_CONTEXT.set(ctx)


def get_current_context(default: Any = None):
    return CURRENT_CONTEXT.get(default)


def reset_current_context(token) -> None:
    CURRENT_CONTEXT.reset(token)


def set_active_window(snapshot: Optional[Dict[str, Any]]):
    return ACTIVE_WINDOW.set(snapshot)


def get_active_window(default: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    return ACTIVE_WINDOW.get(default)


def reset_active_window(token) -> None:
    ACTIVE_WINDOW.reset(token)


def _store_active_window(snapshot: Optional[Dict[str, Any]]) -> None:
    """
    Persist active window info in both the context var and the current TaskContext when available.
    """
    ACTIVE_WINDOW.set(snapshot)
    ctx = CURRENT_CONTEXT.get(None)
    if ctx is not None:
        try:
            ctx.active_window = snapshot
        except Exception:
            try:
                setattr(ctx, "active_window", snapshot)
            except Exception:
                pass


def _get_active_window_snapshot() -> Optional[Dict[str, Any]]:
    ctx = CURRENT_CONTEXT.get(None)
    if ctx is not None:
        snap = getattr(ctx, "active_window", None)
        if snap:
            return snap
    return ACTIVE_WINDOW.get(None)


__all__ = [
    "CURRENT_CONTEXT",
    "ACTIVE_WINDOW",
    "set_current_context",
    "get_current_context",
    "reset_current_context",
    "set_active_window",
    "get_active_window",
    "reset_active_window",
    "_store_active_window",
    "_get_active_window_snapshot",
]
