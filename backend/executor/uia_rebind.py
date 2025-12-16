"""
Helpers to safely rebind UI Automation elements from stored references.
"""

from typing import Any, Dict, Optional

import uiautomation as auto


def _is_usable(element: Any) -> bool:
    """Return True when the element looks interactive and onscreen."""
    try:
        current = getattr(element, "Current", None)
        if current is not None:
            if getattr(current, "IsEnabled", True) is False:
                return False
            if hasattr(current, "IsOffscreen") and getattr(current, "IsOffscreen") is True:
                return False
    except Exception:
        # If we cannot read the state, assume usable and let callers handle failures.
        return True
    return True


def _resolve_root(root: Optional[Any]) -> Optional[Any]:
    """Prefer the active top-level window, otherwise the desktop root."""
    if root is not None:
        return root
    try:
        fg = auto.GetForegroundControl()
        if fg:
            try:
                return fg.GetTopLevelControl()
            except Exception:
                return fg
    except Exception:
        pass
    try:
        return auto.GetRootControl()
    except Exception:
        return None


def _from_runtime_id(runtime_id: Optional[Any]) -> Optional[Any]:
    if not runtime_id:
        return None
    try:
        rid = [int(v) for v in runtime_id]
    except Exception:
        return None
    for attr in ("FromRuntimeId", "FromRuntimeID"):
        ctor = getattr(auto.AutomationElement, attr, None)
        if callable(ctor):
            try:
                element = ctor(rid)
                if element:
                    return element
            except Exception:
                continue
    try:
        return auto.AutomationElement.FromRuntimeId(rid)
    except Exception:
        pass
    try:
        return auto.AutomationElement(runtimeId=rid)
    except Exception:
        return None


def _control_type_from_name(name: Optional[str]) -> Optional[Any]:
    if not name:
        return None
    try:
        return getattr(auto.ControlType, str(name))
    except Exception:
        try:
            normalized = str(name).replace(" ", "")
            return getattr(auto.ControlType, normalized)
        except Exception:
            return None


def _search_by_locator_key(locator_key: Optional[Dict[str, Any]], root: Optional[Any]) -> Optional[Any]:
    if not locator_key:
        return None
    search_root = _resolve_root(root)
    if not search_root:
        return None

    automation_id = locator_key.get("automation_id")
    name = locator_key.get("name")
    control_type = locator_key.get("control_type")
    class_name = locator_key.get("class_name")

    ctrl_norm = str(control_type or "").replace(" ", "").lower()
    name_norm = str(name or "").strip()

    def _matches(element: Any) -> bool:
        try:
            elem_auto = getattr(element, "AutomationId", None)
            elem_name = getattr(element, "Name", None)
            elem_class = getattr(element, "ClassName", None)
            elem_ctrl_name = getattr(element, "ControlTypeName", None)
            elem_ctrl = elem_ctrl_name or getattr(element, "ControlType", None)
            elem_ctrl_norm = str(elem_ctrl_name or elem_ctrl or "").replace(" ", "").lower()
        except Exception:
            return False

        if automation_id and elem_auto and str(elem_auto) == str(automation_id):
            if class_name and elem_class not in {class_name}:
                return False
            if ctrl_norm and elem_ctrl_norm and elem_ctrl_norm != ctrl_norm:
                return False
            return True

        if name_norm and elem_name and str(elem_name).strip() == name_norm:
            if ctrl_norm and elem_ctrl_norm and elem_ctrl_norm != ctrl_norm:
                return False
            if class_name and elem_class not in {class_name}:
                return False
            return True

        if ctrl_norm and elem_ctrl_norm == ctrl_norm:
            if class_name and elem_class not in {class_name}:
                return False
            return True
        return False

    stack = [search_root]
    try:
        stack.extend(list(search_root.GetChildren()))
    except Exception:
        stack = list(stack)
    inspected = 0
    max_nodes = 512
    while stack and inspected < max_nodes:
        element = stack.pop(0)
        inspected += 1
        try:
            stack.extend(list(element.GetChildren()))
        except Exception:
            pass
        try:
            handle = getattr(element, "NativeWindowHandle", None)
            if handle is None and getattr(element, "IsOffscreen", False):
                continue
        except Exception:
            pass
        if _matches(element) and _is_usable(element):
            return element
    return None


def rebind_element(ref: Dict[str, Any], root: Optional[Any] = None) -> Optional[Any]:
    """
    Rebind an AutomationElement using runtime_id first, then locator_key.

    Returns None when the element cannot be recovered or fails safety checks.
    """
    if not isinstance(ref, dict):
        return None

    runtime_id = ref.get("runtime_id")
    locator_key = ref.get("locator_key")

    element = _from_runtime_id(runtime_id)
    if element and _is_usable(element):
        return element

    element = _search_by_locator_key(locator_key, root)
    if element and _is_usable(element):
        return element
    return None
