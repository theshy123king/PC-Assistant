from enum import Enum
from typing import Any, Dict, Optional, Union

import uiautomation as auto
import time


class MatchPolicy(Enum):
    """
    Control how UI Automation matches are selected.

    CONTROL_ONLY: restrict to interactive controls, never return windows.
    WINDOW_FIRST: prefer top-level windows (for activation/switching).
    HYBRID: legacy mixed strategy (default for backward compatibility).
    """

    CONTROL_ONLY = "control_only"
    WINDOW_FIRST = "window_first"
    HYBRID = "hybrid"


def _normalize_policy(policy: Union["MatchPolicy", str, None]) -> "MatchPolicy":
    if isinstance(policy, MatchPolicy):
        return policy
    if policy is None:
        return MatchPolicy.HYBRID
    policy_str = str(policy).strip().lower()
    for candidate in MatchPolicy:
        if policy_str == candidate.value or policy_str == candidate.name.lower():
            return candidate
    raise ValueError(f"Unknown match policy: {policy}")


def _control_type_name(control: Any) -> str:
    """Return a normalized control type name."""
    return str(getattr(control, "ControlTypeName", "") or "").strip().lower()


def _is_window_control(control: Any) -> bool:
    """Return True if the UIA control represents a window container."""
    try:
        if getattr(control, "ControlType", None) == auto.ControlType.WindowControl:
            return True
    except Exception:
        pass
    type_name = _control_type_name(control)
    return type_name in {"windowcontrol", "window"}


def _is_container_control(control: Any) -> bool:
    """
    Returns True for common non-interactive container controls we do not want to click.
    """
    container_types = {
        auto.ControlType.WindowControl,
        auto.ControlType.PaneControl,
        auto.ControlType.TitleBarControl,
        auto.ControlType.ToolBarControl,
        auto.ControlType.GroupControl,
        getattr(auto.ControlType, "DocumentControl", None),
    }
    container_types = {t for t in container_types if t is not None}
    try:
        ctype = getattr(control, "ControlType", None)
        if ctype in container_types:
            return True
    except Exception:
        pass
    type_name = _control_type_name(control)
    return type_name in {
        "windowcontrol",
        "window",
        "panecontrol",
        "pane",
        "titlebarcontrol",
        "titlebar",
        "toolbarcontrol",
        "toolbar",
        "groupcontrol",
        "group",
        "documentcontrol",
        "document",
    }


def _is_interactive_control(control: Any) -> bool:
    """Whitelist of controls that are typically safe to interact with."""
    interactive_types = {
        auto.ControlType.ButtonControl,
        auto.ControlType.EditControl,
        auto.ControlType.MenuItemControl,
        auto.ControlType.TabItemControl,
        auto.ControlType.HyperlinkControl,
        auto.ControlType.ListItemControl,
        auto.ControlType.TreeItemControl,
        auto.ControlType.CheckBoxControl,
        auto.ControlType.RadioButtonControl,
        auto.ControlType.ComboBoxControl,
        getattr(auto.ControlType, "DataItemControl", None),
    }
    interactive_types = {t for t in interactive_types if t is not None}
    try:
        if getattr(control, "ControlType", None) in interactive_types:
            return True
    except Exception:
        pass
    return _control_type_name(control) in {
        "buttoncontrol",
        "button",
        "editcontrol",
        "edit",
        "menuitemcontrol",
        "menuitem",
        "tabitemcontrol",
        "tabitem",
        "hyperlinkcontrol",
        "hyperlink",
        "listitemcontrol",
        "listitem",
        "treeitemcontrol",
        "treeitem",
        "checkboxcontrol",
        "checkbox",
        "radiobuttoncontrol",
        "radiobutton",
        "comboboxcontrol",
        "combobox",
        "dataitemcontrol",
        "dataitem",
    }


def _is_control_allowed(control: Any, policy: MatchPolicy) -> bool:
    """
    Decide whether the control should be returned under the given policy.

    CONTROL_ONLY prefers a conservative whitelist to avoid misclicking container chrome.
    """
    if policy != MatchPolicy.CONTROL_ONLY:
        return True
    if _is_window_control(control) or _is_container_control(control):
        return False
    return _is_interactive_control(control)


def _resolve_preferred_root(
    preferred_hwnd: Optional[int],
    preferred_pid: Optional[int],
    preferred_title: Optional[str],
    query_norm: str,
) -> Any:
    """
    Return a preferred root element from hwnd or pid.

    Raises ValueError when a preferred root is provided but cannot be resolved.
    """
    if preferred_hwnd is not None:
        try:
            hwnd_int = int(preferred_hwnd)
            ctrl = auto.ControlFromHandle(hwnd_int)
            if ctrl:
                try:
                    return ctrl.GetTopLevelControl()
                except Exception:
                    return ctrl
        except Exception:
            pass
        raise ValueError("preferred_root_unavailable:hwnd")

    if preferred_pid is not None:
        try:
            desktop = auto.GetRootControl()
            if desktop:
                best = None
                best_score = -1.0
                for win in desktop.GetChildren():
                    try:
                        if getattr(win, "ProcessId", None) != int(preferred_pid):
                            continue
                        score = 0.0
                        try:
                            name = str(getattr(win, "Name", "") or "").lower()
                            if preferred_title:
                                if preferred_title.lower() == name:
                                    score += 2.0
                                elif preferred_title.lower() in name or name in preferred_title.lower():
                                    score += 1.2
                            if query_norm and query_norm in name:
                                score += 1.0
                        except Exception:
                            pass
                        if getattr(win, "IsOffscreen", False):
                            score -= 0.2
                        if score > best_score:
                            best_score = score
                            best = win
                    except Exception:
                        continue
                if best:
                    try:
                        return best.GetTopLevelControl()
                    except Exception:
                        return best
        except Exception:
            pass
        raise ValueError("preferred_root_unavailable:pid")
    return None


def find_element(
    query: str,
    root=None,
    timeout: float = 1.0,
    policy: Union[MatchPolicy, str] = MatchPolicy.HYBRID,
    preferred_hwnd: Optional[int] = None,
    preferred_pid: Optional[int] = None,
    preferred_title: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Hybrid UIA locator with policy-aware window/control selection.

    1) WINDOW_FIRST/HYBRID: search top-level windows without visibility filtering.
    2) Search controls inside the foreground window (visible only).
    3) Allow caller to pass a custom root for scoped search.
    4) Prefer an explicit hwnd/pid root when provided.
    """
    policy_enum = _normalize_policy(policy)
    query_norm = str(query).strip().lower()
    preferred_root = None
    with auto.UIAutomationInitializerInThread(debug=False):
        start_ts = time.time()
        print(
            f"[DEBUG] ({start_ts:.3f}) find_element start query='{query_norm}' policy='{policy_enum.value}'"
        )
        window_hit: Optional[Dict[str, Any]] = None

        # Strategy 4/3: preferred or explicit root
        if preferred_hwnd is not None or preferred_pid is not None:
            preferred_root = _resolve_preferred_root(preferred_hwnd, preferred_pid, preferred_title, query_norm)
        search_root = preferred_root or root
        if search_root:
            root_hwnd = getattr(search_root, "NativeWindowHandle", None)
            root_cls = getattr(search_root, "ClassName", None) or getattr(search_root, "ControlTypeName", "")
            print(
                f"[DEBUG] ({time.time():.3f}) Search Root Identified: "
                f"{getattr(search_root, 'Name', '(unknown)')} ({root_cls}) [hwnd={root_hwnd}]"
                f"{' (preferred)' if preferred_root else ' (explicit)'}"
            )
            if policy_enum != MatchPolicy.CONTROL_ONLY and query_norm and getattr(search_root, "Name", None):
                name_norm = str(getattr(search_root, "Name", "") or "").strip().lower()
                if query_norm in name_norm and not _is_window_control_excluded(search_root, policy_enum):
                    return _pack_result(search_root, kind="window", method="uia", score=1.0)
            try:
                return _search_in_root(search_root, query_norm, policy_enum)
            except Exception as exc:
                raise ValueError(f"uia_root_search_failed:{exc}") from exc

        # Strategy 1: top-level windows (only when allowed)
        if policy_enum != MatchPolicy.CONTROL_ONLY:
            desktop = auto.GetRootControl()
            for win in desktop.GetChildren():
                try:
                    raw_name = win.Name
                    if not raw_name:
                        continue
                    name = str(raw_name).strip()
                    if not name:
                        continue
                    cls = getattr(win, "ClassName", None) or getattr(
                        win, "ControlTypeName", ""
                    )
                    print(
                        f"[DEBUG] ({time.time():.3f}) Checking Window: '{name}' ({cls})"
                    )
                    name_norm = name.lower()
                    if query_norm in name_norm and not _is_window_control_excluded(
                        win, policy_enum
                    ):
                        print(f"[MATCH] ({time.time():.3f}) Hit Window: {name}")
                        if policy_enum == MatchPolicy.WINDOW_FIRST:
                            return _pack_result(win, kind="window", method="uia", score=1.0)
                        if policy_enum == MatchPolicy.HYBRID and window_hit is None:
                            # Apply penalty so controls can win when both exist.
                            window_hit = _pack_result(win, kind="window", method="uia", score=0.4)
                except Exception:
                    continue

        # Strategy 2: search within the foreground window (visible controls only)
        try:
            foreground = auto.GetForegroundControl()
            if foreground:
                try:
                    top_window = foreground.GetTopLevelControl()
                    print(
                        f"[DEBUG] ({time.time():.3f}) Search Root Identified: "
                        f"{getattr(top_window, 'Name', '(unknown)')}"
                    )
                except Exception:
                    top_window = None
                if top_window:
                    result = _search_in_root(top_window, query_norm, policy_enum)
                    if result:
                        return result
        except Exception:
            pass

        # HYBRID: fall back to penalized window match only if no control found.
        if window_hit and policy_enum == MatchPolicy.HYBRID:
            return window_hit

        print(f"[DEBUG] ({time.time():.3f}) find_element end: no match for '{query_norm}'")
        return None


def _search_in_root(root, query_norm: str, policy: MatchPolicy) -> Optional[Dict[str, Any]]:
    """Search visible controls under a given root without relying on PropertyCondition."""
    try:
        children = list(root.GetChildren())
    except Exception:
        children = []
    stack = children[:]
    seen = set()
    print(f"[DEBUG] ({time.time():.3f}) _search_in_root query='{query_norm}' start children={len(stack)}")

    while stack:
        element = stack.pop(0)
        try:
            handle = getattr(element, "NativeWindowHandle", None)
            if handle and handle in seen:
                continue
            if handle:
                seen.add(handle)
            if getattr(element, "IsOffscreen", False):
                pass
            if not _is_control_allowed(element, policy):
                # Still traverse its children for nested matches.
                try:
                    stack.extend(list(element.GetChildren()))
                except Exception:
                    pass
                continue
            name = str(getattr(element, "Name", "") or "").strip()
            if not name:
                try:
                    stack.extend(list(element.GetChildren()))
                except Exception:
                    pass
                continue
            ctype = getattr(element, "ControlTypeName", "")
            name_l = name.lower()
            if query_norm in name_l:
                rect = getattr(element, "BoundingRectangle", None)
                rect_str = f"{rect.left},{rect.top},{rect.right},{rect.bottom}" if rect else "n/a"
                print(
                    f"[MATCH] ({time.time():.3f}) Found: Name='{name}', "
                    f"ControlType='{ctype}', BBox={rect_str}"
                )
                return _pack_result(element, kind="control", method="uia", score=1.0)
            try:
                stack.extend(list(element.GetChildren()))
            except Exception:
                continue
        except Exception:
            continue

    return None


def _is_window_control_excluded(control: Any, policy: MatchPolicy) -> bool:
    """
    Helper to gate window controls based on policy.

    CONTROL_ONLY refuses windows to prevent misclicks on container centers.
    """
    return policy == MatchPolicy.CONTROL_ONLY and (
        _is_window_control(control) or _is_container_control(control)
    )


def _pack_result(
    element: Any,
    kind: str,
    method: str = "uia",
    score: Optional[float] = None,
) -> Dict[str, Any]:
    """Normalize the return structure for UIA results."""
    rect = getattr(element, "BoundingRectangle", None)
    rect_payload = None
    bbox = None
    center = None
    if rect:
        try:
            rect_payload = {
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
            }
            bbox = {
                "x": rect.left,
                "y": rect.top,
                "width": rect.width(),
                "height": rect.height(),
            }
            center = {
                "x": (rect.left + rect.right) // 2,
                "y": (rect.top + rect.bottom) // 2,
            }
        except Exception:
            rect_payload = None
            bbox = None
            center = None

    try:
        runtime_id = element.GetRuntimeId() if hasattr(element, "GetRuntimeId") else getattr(element, "RuntimeId", None)
    except Exception:
        runtime_id = None
    if runtime_id:
        try:
            runtime_id = [int(x) for x in runtime_id]
        except Exception:
            runtime_id = None
    try:
        automation_id = getattr(element, "AutomationId", None)
    except Exception:
        automation_id = None
    try:
        class_name = getattr(element, "ClassName", None)
    except Exception:
        class_name = None
    try:
        handle = element.NativeWindowHandle
    except Exception:
        handle = None
    try:
        pid = getattr(element, "ProcessId", None)
    except Exception:
        pid = None
    locator_key = {
        "name": getattr(element, "Name", None),
        "automation_id": automation_id,
        "control_type": getattr(element, "ControlTypeName", None),
        "class_name": class_name,
    }
    locator_key = {k: v for k, v in locator_key.items() if v not in (None, "")}
    target_ref = None
    if runtime_id or locator_key:
        target_ref = {"runtime_id": runtime_id, "locator_key": locator_key or None}

    result: Dict[str, Any] = {
        "method": method,
        "kind": "window" if kind == "window" else "control",
        "name": getattr(element, "Name", None),
        "control_type": getattr(element, "ControlTypeName", None),
        "rect": rect_payload,
        "runtime_id": runtime_id,
        "automation_id": automation_id,
        "pid": pid,
        "locator_key": locator_key or None,
        "handle": handle,
        "score": score,
    }
    if target_ref:
        result["target_ref"] = target_ref
    if bbox:
        result["bbox"] = bbox
    if center:
        result["center"] = center
    return result
