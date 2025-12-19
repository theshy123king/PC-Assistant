from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from backend.executor.actions_schema import ActionStep


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


def handle_hotkey(step: ActionStep) -> str:
    params = step.params or {}
    keys = params.get("keys") or params.get("key")
    normalized = []
    if isinstance(keys, str):
        normalized = [k for k in keys.split("+") if k]
    elif isinstance(keys, (list, tuple)):
        normalized = [str(k) for k in keys if k]
    if not normalized:
        return "error: 'keys' param is required (string or list)"
    try:
        import pyautogui  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return f"error: pyautogui unavailable: {exc}"

    try:
        if len(normalized) == 1:
            pyautogui.press(normalized[0])
            return f"pressed hotkey {normalized[0]}"
        pyautogui.hotkey(*normalized)
        return f"pressed hotkey {'+'.join(normalized)}"
    except Exception as exc:  # noqa: BLE001
        return f"error: failed to press hotkey: {exc}"


__all__ = ["Dispatcher", "handle_hotkey"]
