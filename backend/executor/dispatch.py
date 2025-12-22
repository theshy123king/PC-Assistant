from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from backend.executor.actions_schema import ActionStep
from backend.executor import input


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


def handle_type(step: ActionStep) -> str:
    return input.type_text(step.params)


def handle_click(step: ActionStep, *, provider: Any) -> Any:
    """
    Basic click handler. Dependencies are passed in via `provider` to avoid
    importing executor from dispatch while keeping monkey-patching behavior.
    """
    from pathlib import Path

    from backend.vision.uia_locator import MatchPolicy

    params = step.params or {}
    button = params.get("button", "left")
    _coerce_bool = getattr(provider, "_coerce_bool")
    _get_active_window_snapshot = getattr(provider, "_get_active_window_snapshot")
    ACTIVE_WINDOW = getattr(provider, "ACTIVE_WINDOW")
    _foreground_snapshot = getattr(provider, "_foreground_snapshot")
    MOUSE = getattr(provider, "MOUSE")
    _extract_targets = getattr(provider, "_extract_targets")
    _enforce_strict_foreground_once = getattr(provider, "_enforce_strict_foreground_once")
    _ensure_foreground = getattr(provider, "_ensure_foreground")
    _capture_for_interaction = getattr(provider, "_capture_for_interaction")
    run_ocr_with_boxes = getattr(provider, "run_ocr_with_boxes")
    _locate_from_params = getattr(provider, "_locate_from_params")
    _extract_center_from_locator = getattr(provider, "_extract_center_from_locator")
    _validate_locator_center = getattr(provider, "_validate_locator_center")
    _execute_click_strategies = getattr(provider, "_execute_click_strategies")
    InteractionStrategyError = getattr(provider, "InteractionStrategyError")
    _extract_target_ref_from_locator = getattr(provider, "_extract_target_ref_from_locator")
    _store_active_window = getattr(provider, "_store_active_window")

    strict_fg = _coerce_bool(params.get("strict_foreground"), False)
    preferred_window = _get_active_window_snapshot() or (ACTIVE_WINDOW.get(None) or {})
    if strict_fg and not preferred_window.get("hwnd"):
        preferred_window = _foreground_snapshot()
    preferred = preferred_window
    pref_hwnd = preferred.get("hwnd") or preferred.get("handle")
    try:
        pref_hwnd = int(pref_hwnd) if pref_hwnd is not None else None
    except Exception:
        pref_hwnd = None
    if strict_fg and not pref_hwnd:
        return {"status": "error", "reason": "preferred_window_unavailable", "preferred": preferred}

    x = params.get("x")
    y = params.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        print(f"[EXEC] Clicking at ({x}, {y})")
        result = MOUSE.click({"x": x, "y": y, "button": button})
        return {"status": "success", "method": "absolute", "reason": result}

    targets = _extract_targets(params)
    if not targets:
        return "error: 'x'/'y' or 'text/target/visual_description' is required"

    logs = []
    if strict_fg:
        fg_ok, fg_after, enforcement = _enforce_strict_foreground_once(preferred, logs=logs)
        if not fg_ok:
            try:
                _store_active_window(None)
            except Exception:
                pass
            return {
                "status": "error",
                "reason": "foreground_mismatch",
                "foreground": fg_after,
                "preferred": preferred,
                "enforcement": enforcement,
            }
    else:
        _ensure_foreground(preferred, strict_fg, logs=logs)

    screenshot_path, fg_after, capture_err = _capture_for_interaction(preferred, strict_fg)
    if capture_err:
        return {"status": "error", "reason": capture_err, "foreground": fg_after, "preferred": preferred}

    try:
        _full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": f"ocr failed: {exc}"}

    locate_result = None
    for query in targets:
        print(f"[EXEC] Trying variant: '{query}'")
        try:
            locate_result = _locate_from_params(
                query, params, boxes, Path(screenshot_path), match_policy=MatchPolicy.CONTROL_ONLY
            )
        except ValueError as exc:
            return {"status": "error", "reason": str(exc)}
        if locate_result.get("status") == "success":
            break

    if not locate_result or locate_result.get("status") != "success":
        reason = (locate_result or {}).get("reason") if isinstance(locate_result, dict) else None
        return {"status": "error", "reason": reason or "locate_failed", "locator": locate_result}
    if strict_fg and locate_result.get("method") == "vlm":
        return {"status": "error", "reason": "unverified_click_vlm_strict", "locator": locate_result}
    center = _extract_center_from_locator(locate_result)
    valid_center, center_reason = _validate_locator_center(center, locate_result)
    if not valid_center:
        return {"status": "error", "reason": center_reason, "locator": locate_result}
    locate_result["center"] = center
    try:
        return _execute_click_strategies(locate_result, button=button)
    except InteractionStrategyError as exc:
        target_ref = getattr(exc, "target_ref", None) or _extract_target_ref_from_locator(locate_result)
        rebind_meta = getattr(exc, "rebind_meta", {})
        return {
            "status": "error",
            "reason": str(exc),
            "locator": locate_result,
            "target_ref": target_ref,
            "message": {
                "ok": False,
                "status": "error",
                "method": locate_result.get("method"),
                "pattern": None,
                "rebind": rebind_meta,
                "reason": str(exc),
            },
        }


def handle_open_app(step: ActionStep, *, provider: Any) -> Any:
    apps = getattr(provider, "apps")
    result = apps.open_app(step.params)
    last_window_attr = "LAST_WINDOW_TITLE"
    last_context_attr = "LAST_OPEN_APP_CONTEXT"
    if isinstance(result, dict):
        if result.get("window_title") is not None and hasattr(provider, last_window_attr):
            try:
                setattr(provider, last_window_attr, result.get("window_title"))
            except Exception:
                pass
        target = (step.params or {}).get("target") or result.get("target")
        kind = result.get("selected_kind")
        launched_pid = result.get("pid") or result.get("process_id")
        context_payload = {
            "target": str(target).lower() if target else None,
            "selected_kind": kind,
            "window_title": result.get("window_title"),
            "pid": launched_pid,
        }
        if hasattr(provider, last_context_attr):
            try:
                setattr(provider, last_context_attr, context_payload)
            except Exception:
                pass
    return result


__all__ = ["Dispatcher", "handle_hotkey", "handle_type", "handle_click", "handle_open_app"]


def handle_wait_until(step: ActionStep, *, provider: Any) -> Dict[str, Any]:
    from pathlib import Path
    import time

    MatchPolicy = getattr(provider, "MatchPolicy")
    WaitUntilAction = getattr(provider, "WaitUntilAction")
    ValidationError = getattr(provider, "ValidationError")
    capture_screen = getattr(provider, "capture_screen")
    find_element = getattr(provider, "find_element")
    locate_target = getattr(provider, "locate_target")
    run_ocr_with_boxes = getattr(provider, "run_ocr_with_boxes")
    rank_text_candidates = getattr(provider, "rank_text_candidates")
    _preferred_window_hint = getattr(provider, "_preferred_window_hint")
    _reject_window_match = getattr(provider, "_reject_window_match")
    _get_active_window_rect = getattr(provider, "_get_active_window_rect")
    _hash_active_window_region = getattr(provider, "_hash_active_window_region")
    CURRENT_CONTEXT = getattr(provider, "CURRENT_CONTEXT")

    params = step.params or {}
    try:
        wait_action = WaitUntilAction.model_validate(params)
    except ValidationError as exc:  # noqa: BLE001
        return {
            "status": "timeout",
            "condition": params.get("condition"),
            "reason": f"invalid wait_until params: {exc}",
            "elapsed": 0.0,
            "polls": 0,
        }

    start = time.monotonic()
    polls = 0
    last_observed: Any = None
    last_reason: Optional[str] = None
    stable_value: Optional[str] = None
    stable_method: Optional[str] = None
    stable_count = 0
    last_change_ts = start
    preferred_window = _preferred_window_hint()

    def _current_ui_state() -> tuple[Optional[str], str, Optional[str]]:
        ctx = CURRENT_CONTEXT.get(None)
        if ctx and hasattr(ctx, "get_ui_fingerprint"):
            try:
                fp = ctx.get_ui_fingerprint(lite_only=True)
            except Exception:
                fp = None
            if fp:
                return str(fp), "fingerprint", None
        rect = _get_active_window_rect()
        if not rect:
            return None, "hash", "no_active_window_rect"
        digest, err = _hash_active_window_region(rect)
        return digest, "hash", err

    while True:
        polls += 1
        now = time.monotonic()
        elapsed = now - start

        if wait_action.condition == "window_exists":
            res = find_element(
                wait_action.target,
                policy=MatchPolicy.WINDOW_FIRST,
                preferred_hwnd=preferred_window.get("preferred_hwnd"),
                preferred_pid=preferred_window.get("preferred_pid"),
                preferred_title=preferred_window.get("preferred_title"),
            )
            last_observed = res
            if res and res.get("kind") == "window":
                return {
                    "status": "success",
                    "ok": True,
                    "condition": wait_action.condition,
                    "elapsed": elapsed,
                    "matched_target": res,
                    "polls": polls,
                }
        elif wait_action.condition == "element_exists":
            screenshot_path: Optional[Path] = None
            boxes: List[Any] = []
            try:
                screenshot_path = Path(capture_screen())
                _full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
            except Exception as exc:  # noqa: BLE001
                last_reason = f"ocr_failed:{exc}"
            try:
                locate_result = locate_target(
                    query=wait_action.target,
                    boxes=boxes,
                    image_path=str(screenshot_path) if screenshot_path else None,
                    match_policy=MatchPolicy.CONTROL_ONLY,
                    preferred_hwnd=preferred_window.get("preferred_hwnd"),
                    preferred_pid=preferred_window.get("preferred_pid"),
                    preferred_title=preferred_window.get("preferred_title"),
                )
                _reject_window_match(locate_result, wait_action.target or "")
                last_observed = locate_result
                if locate_result.get("status") == "success":
                    return {
                        "status": "success",
                        "ok": True,
                        "condition": wait_action.condition,
                        "elapsed": elapsed,
                        "matched_target": locate_result,
                        "polls": polls,
                    }
            except ValueError as exc:
                last_reason = str(exc)
        elif wait_action.condition == "text_exists":
            screenshot_path = None
            boxes: List[Any] = []
            try:
                screenshot_path = Path(capture_screen())
                _full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
            except Exception as exc:  # noqa: BLE001
                last_reason = f"ocr_failed:{exc}"
            candidates = rank_text_candidates(wait_action.target or "", boxes)
            last_observed = candidates[0] if candidates else None
            if candidates:
                best = candidates[0]
                if best.get("high_enough") or best.get("medium_enough"):
                    return {
                        "status": "success",
                        "ok": True,
                        "condition": wait_action.condition,
                        "elapsed": elapsed,
                        "matched_target": best,
                        "polls": polls,
                    }
        elif wait_action.condition == "ui_stable":
            value, method, reason = _current_ui_state()
            last_observed = {"value": value, "method": method, "reason": reason}
            if reason:
                last_reason = reason
            if value:
                if value == stable_value and method == stable_method:
                    stable_count += 1
                else:
                    stable_value = value
                    stable_method = method
                    stable_count = 1
                    last_change_ts = now
                if stable_count >= wait_action.stable_samples or (now - last_change_ts) >= wait_action.stability_duration:
                    return {
                        "status": "success",
                        "ok": True,
                        "condition": wait_action.condition,
                        "elapsed": elapsed,
                        "matched_target": last_observed,
                        "polls": polls,
                    }
            else:
                stable_count = 0
        else:
            last_reason = f"unsupported_condition:{wait_action.condition}"

        if elapsed >= wait_action.timeout:
            break
        time.sleep(wait_action.poll_interval)

    timeout_result = {
        "status": "timeout",
        "ok": False,
        "condition": wait_action.condition,
        "elapsed": elapsed,
        "last_observed": last_observed,
        "reason": last_reason or "timeout",
        "polls": polls,
        "timeout_allowed": bool(getattr(wait_action, "allow_timeout", False)),
    }
    if wait_action.require:
        raise RuntimeError(f"wait_until failed: condition '{wait_action.condition}' not met within {wait_action.timeout}s")
    return timeout_result


__all__ = [
    "Dispatcher",
    "handle_hotkey",
    "handle_type",
    "handle_click",
    "handle_open_app",
    "handle_wait_until",
]
