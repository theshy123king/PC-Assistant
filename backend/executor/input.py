"""
Input helpers for simulated typing and key presses.

Provides thin wrappers around pyautogui with soft error handling.
"""

from typing import Any, Dict, Iterable, List
import time

import pyautogui


def _toggle_ime_to_ascii() -> None:
    """
    Best-effort IME toggle to half-width/English.

    Tries multiple common shortcuts: Shift+Space, Ctrl+Space, and a plain Shift tap
    for users who switch layouts with Shift.
    """
    try:
        pyautogui.hotkey("shift", "space")
    except Exception:
        pass
    try:
        pyautogui.hotkey("ctrl", "space")
    except Exception:
        pass
    try:
        pyautogui.press("shift")
    except Exception:
        pass


def type_text(params: Dict[str, Any]) -> str:
    """
    Type the given text with a small interval between characters.

    Expected params:
        text: str - text to type.
        interval: float (optional) - delay between keystrokes.
        auto_enter: bool (optional, default True) - press Enter after typing.

    Returns:
        A status string describing success or the encountered error.
    """
    text = (params or {}).get("text")
    if not isinstance(text, str) or not text:
        return "error: 'text' param is required and must be non-empty string"

    interval = params.get("interval", 0.02) if isinstance(params, dict) else 0.02

    def _coerce_auto_enter(raw: Any) -> bool:
        if raw is None:
            return True
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        val = str(raw).strip().lower()
        if val in {"false", "0", "no", "off"}:
            return False
        if val in {"true", "1", "yes", "on"}:
            return True
        return True

    def _coerce_flag(raw: Any, default: bool = False) -> bool:
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        val = str(raw).strip().lower()
        if val in {"false", "0", "no", "off"}:
            return False
        if val in {"true", "1", "yes", "on"}:
            return True
        return default

    auto_enter = _coerce_auto_enter((params or {}).get("auto_enter"))
    force_ascii = _coerce_flag(
        (params or {}).get("force_ascii")
        or (params or {}).get("ime_safe")
        or (params or {}).get("ime_half_width")
        or ((params or {}).get("mode") == "filename"),
        False,
    )
    # Heuristic: if text looks like a filename (contains a dot), force ASCII + Enter to commit.
    is_filename = force_ascii or ("." in text) or ((params or {}).get("mode") == "filename")
    if is_filename:
        force_ascii = True
        auto_enter = True  # ensure save dialog commits

    try:
        if force_ascii:
            try:
                pyautogui.press("esc")
            except Exception:
                pass
            _toggle_ime_to_ascii()
            # Clear any prefilled text/spaces before typing filename.
            try:
                pyautogui.hotkey("ctrl", "a")
                pyautogui.press("backspace")
            except Exception:
                pass

        pyautogui.typewrite(text, interval=interval)
        # Commit IME composition to ensure subsequent shortcuts reach the app.
        if force_ascii:
            try:
                pyautogui.press("enter")
            except Exception:
                pass
        elif auto_enter:
            try:
                pyautogui.press("enter")
            except Exception:
                pass
        else:
            try:
                pyautogui.press("space")
            except Exception:
                pass
        return f"typed {len(text)} characters"
    except Exception as exc:  # noqa: BLE001
        return f"error: failed to type text: {exc}"


def _normalize_keys(raw_keys: Any) -> List[str]:
    if isinstance(raw_keys, str):
        return [k.strip().lower() for k in raw_keys.split("+") if k]
    if isinstance(raw_keys, Iterable):
        return [str(k).strip().lower() for k in raw_keys if str(k).strip()]
    return []


def key_press(params: Dict[str, Any]) -> str:
    """
    Press keys or key combos.

    Expected params:
        keys: str or list - single key (e.g., "enter") or combo "ctrl+s"/["ctrl","s"].
        post_delay: float (optional) - sleep seconds after pressing (default 0.2 for combos, 0 otherwise).

    Returns:
        A status string describing success or the encountered error.
    """
    keys = _normalize_keys((params or {}).get("keys") or (params or {}).get("key"))
    if not keys:
        return "error: 'keys' param is required (string or list)"

    def _coerce_flag(raw: Any, default: bool = False) -> bool:
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        val = str(raw).strip().lower()
        if val in {"false", "0", "no", "off"}:
            return False
        if val in {"true", "1", "yes", "on"}:
            return True
        return default

    force_ascii = _coerce_flag(
        (params or {}).get("force_ascii") or (params or {}).get("ime_safe") or (params or {}).get("ime_half_width")
    )
    is_save_combo = keys == ["ctrl", "s"]
    # Auto-enable IME safe mode for Ctrl+S to help save dialogs.
    if not force_ascii and is_save_combo:
        force_ascii = True

    post_delay = 0.0
    if isinstance(params, dict) and "post_delay" in params:
        try:
            post_delay = float(params.get("post_delay"))
        except Exception:
            post_delay = 0.0
    try:
        if force_ascii:
            _toggle_ime_to_ascii()

        if len(keys) == 1:
            pyautogui.press(keys[0])
            if post_delay > 0:
                time.sleep(post_delay)
            return f"pressed '{keys[0]}'"
        pyautogui.hotkey(*keys)
        if post_delay <= 0:
            post_delay = 0.4 if is_save_combo else 0.3
        if post_delay > 0:
            time.sleep(post_delay)
        if is_save_combo:
            # Fallback to menu-based save to force the dialog when hotkey is intercepted.
            try:
                pyautogui.hotkey("alt", "f")
                time.sleep(0.15)
                pyautogui.press("a")
            except Exception:
                pass
        return f"pressed combo '{'+'.join(keys)}'"
    except Exception as exc:  # noqa: BLE001
        return f"error: failed to press keys: {exc}"
