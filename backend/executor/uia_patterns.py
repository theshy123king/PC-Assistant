"""
Thin wrappers for UIA pattern execution with defensive error handling.
"""

from typing import Any, Tuple

import uiautomation as auto


def _get_pattern(element: Any, pattern_id_attr: str, attr_name: str) -> Any:
    """Best-effort pattern fetch using pattern id or direct attributes."""
    pattern = None
    pattern_id = getattr(auto, pattern_id_attr, None)
    getter = getattr(element, "GetCurrentPattern", None)
    if callable(getter) and pattern_id is not None:
        try:
            pattern = getter(pattern_id)
        except Exception:
            pattern = None
    if pattern is None:
        direct = getattr(element, attr_name, None)
        if direct is not None:
            pattern = direct
    alt_getter = getattr(element, f"Get{attr_name}", None)
    if pattern is None and callable(alt_getter):
        try:
            pattern = alt_getter()
        except Exception:
            pattern = None
    return pattern


def try_invoke(element: Any) -> Tuple[bool, str]:
    """Invoke UIA element if possible."""
    try:
        pattern = _get_pattern(element, "UIA_InvokePatternId", "InvokePattern")
        if not pattern or not hasattr(pattern, "Invoke"):
            return False, "invoke_pattern_missing"
        pattern.Invoke()
        return True, "invoke_ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"invoke_failed:{exc}"


def try_set_value(element: Any, text: str) -> Tuple[bool, str]:
    """Set element value if ValuePattern is available and writable."""
    try:
        pattern = _get_pattern(element, "UIA_ValuePatternId", "ValuePattern")
        if not pattern or not hasattr(pattern, "SetValue"):
            return False, "value_pattern_missing"
        readonly = False
        try:
            readonly = bool(getattr(pattern, "CurrentIsReadOnly", False))
        except Exception:
            try:
                readonly = bool(getattr(getattr(pattern, "Current", None), "IsReadOnly", False))
            except Exception:
                readonly = False
        if readonly:
            return False, "value_pattern_readonly"
        pattern.SetValue(str(text))
        return True, "value_ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"value_failed:{exc}"


def try_toggle(element: Any) -> Tuple[bool, str]:
    """Toggle element if supported."""
    try:
        pattern = _get_pattern(element, "UIA_TogglePatternId", "TogglePattern")
        if not pattern or not hasattr(pattern, "Toggle"):
            return False, "toggle_pattern_missing"
        pattern.Toggle()
        return True, "toggle_ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"toggle_failed:{exc}"


def try_select(element: Any) -> Tuple[bool, str]:
    """Select element via SelectionItemPattern."""
    try:
        pattern = _get_pattern(element, "UIA_SelectionItemPatternId", "SelectionItemPattern")
        if not pattern:
            return False, "selection_pattern_missing"
        if hasattr(pattern, "Select"):
            pattern.Select()
            return True, "select_ok"
        if hasattr(pattern, "AddToSelection"):
            pattern.AddToSelection()
            return True, "add_selection_ok"
        return False, "selection_pattern_missing"
    except Exception as exc:  # noqa: BLE001
        return False, f"select_failed:{exc}"


def try_focus(element: Any) -> Tuple[bool, str]:
    """Set focus to the element if available."""
    try:
        if hasattr(element, "SetFocus"):
            element.SetFocus()
            return True, "focus_ok"
        return False, "focus_missing"
    except Exception as exc:  # noqa: BLE001
        return False, f"focus_failed:{exc}"
