from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional


class Dispatcher:
    """
    Minimal forwarding shell to route actions to handlers.

    This is intentionally thin to avoid behavior changes; it only selects the
    appropriate handler mapping (test vs. default) and invokes the callable.
    """

    def __init__(
        self,
        handlers: Mapping[str, Callable[..., Any]],
        test_mode_handlers: Optional[Mapping[str, Callable[..., Any]]] = None,
        *,
        test_mode: bool = False,
    ) -> None:
        self._handlers = handlers or {}
        self._test_handlers = test_mode_handlers or {}
        self.test_mode = bool(test_mode)

    def get_handler(self, action_key: str) -> Optional[Callable[..., Any]]:
        if self.test_mode and self._test_handlers:
            handler = self._test_handlers.get(action_key)
            if handler:
                return handler
        return self._handlers.get(action_key)

    def dispatch(self, action_key: str, *args, **kwargs) -> Any:
        handler = self.get_handler(action_key)
        if handler is None:
            return None
        return handler(*args, **kwargs)


__all__ = ["Dispatcher"]
