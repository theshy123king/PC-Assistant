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


def handle_open_url(step: ActionStep, *, provider: Any) -> Any:
    """
    Open an HTTP/HTTPS URL with the system default browser.
    """
    from urllib.parse import urlparse
    import os
    import shutil
    import subprocess
    import webbrowser

    apps = getattr(provider, "apps")
    LAST_OPEN_APP_CONTEXT = getattr(provider, "LAST_OPEN_APP_CONTEXT")
    _summarize_browser_extract_params = getattr(provider, "_summarize_browser_extract_params")
    _normalize_target_list = getattr(provider, "_normalize_target_list")
    _wait_for_ocr_targets = getattr(provider, "_wait_for_ocr_targets")
    gw = getattr(provider, "gw")

    params = step.params or {}
    _summarize_browser_extract_params(params)
    raw_url = params.get("url") or params.get("target")
    raw_browser = params.get("browser") or params.get("app")
    verify_targets = _normalize_target_list(params.get("verify_text"))
    verify_attempts = params.get("verify_attempts", 3)
    if not raw_url or not isinstance(raw_url, str):
        return "error: 'url' param is required"

    url = raw_url.strip()
    if not url:
        return "error: 'url' param is required"

    parsed = urlparse(url)
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)

    if parsed.scheme.lower() not in {"http", "https"}:
        return "error: only http/https URLs are supported"

    def _aliases(name: str) -> list[str]:
        key = name.lower().strip()
        aliases = [key]
        if "edge" in key:
            aliases.extend(["edge", "msedge", "microsoft edge"])
        if "chrome" in key or "google" in key:
            aliases.extend(["chrome", "google chrome", "chrome.exe"])
        return list(dict.fromkeys(aliases))

    def _find_browser_path(names: list[str]) -> Optional[str]:
        for name in names:
            if not name:
                continue
            for app_key, path in apps.APP_PATHS.items():
                if name in app_key or app_key in name:
                    if os.path.isfile(path):
                        return path
            if os.path.isfile(name):
                return name
            which = shutil.which(name)
            if which:
                return which
        return None

    explicit_browser = None
    browser_hints: list[str] = []
    if isinstance(raw_browser, str) and raw_browser.strip():
        explicit_browser = raw_browser.strip()
        browser_hints.extend(_aliases(explicit_browser))
    elif LAST_OPEN_APP_CONTEXT.get("target"):
        browser_hints.extend(_aliases(str(LAST_OPEN_APP_CONTEXT["target"])))

    browser_path = _find_browser_path(browser_hints) if browser_hints else None
    if browser_path:
        try:
            subprocess.Popen(
                [browser_path, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {
                "status": "opened",
                "url": url,
                "opened": True,
                "browser": browser_path,
                "method": "direct_exec",
            }
        except Exception as exc:  # noqa: BLE001
            if explicit_browser:
                return f"error: failed to open url with browser '{explicit_browser}': {exc}"
            # fall back to default browser below

    if explicit_browser and not browser_path:
        return f"error: browser '{explicit_browser}' not found"

    result: Dict[str, Any] = {"status": "opened", "url": url, "opened": False, "method": None}

    # If we have a direct browser path, we already returned above. If not, try to find an active browser window to use OCR.
    try:
        windows = gw.getAllWindows()
    except Exception:
        windows = []

    active_browser_window = None
    for win in windows:
        title = (getattr(win, "title", "") or "").lower()
        if any(term in title for term in ["edge", "chrome", "firefox", "safari", "浏览器"]):
            active_browser_window = win
            break

    # If no active browser window, force direct launch via default handler.
    if not active_browser_window:
        try:
            opened = webbrowser.open(url)
            result.update({"opened": bool(opened), "method": "default"})
            return result
        except Exception as exc:  # noqa: BLE001
            return f"error: failed to open url: {exc}"

    # Browser window is present: use OCR targeting for address bar if verify_text provided.
    try:
        active_browser_window.activate()
    except Exception:
        pass

    try:
        opened = webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        return f"error: failed to open url: {exc}"

    result.update({"opened": bool(opened), "method": "default_with_active_browser"})
    if verify_targets:
        verify = _wait_for_ocr_targets(verify_targets, attempts=verify_attempts, delay=0.8)
        result["verification"] = verify
        result["verified"] = bool(verify.get("success"))
    return result


def handle_browser_click(step: ActionStep, *, provider: Any) -> Any:
    """
    OCR-driven click helper for browser UI elements.
    """
    from pathlib import Path

    Image = getattr(provider, "Image")
    ACTIVE_WINDOW = getattr(provider, "ACTIVE_WINDOW")
    MatchPolicy = getattr(provider, "MatchPolicy")
    _extract_targets = getattr(provider, "_extract_targets")
    _normalize_target_list = getattr(provider, "_normalize_target_list")
    _preferred_window_hint = getattr(provider, "_preferred_window_hint")
    _get_active_window_snapshot = getattr(provider, "_get_active_window_snapshot")
    _coerce_bool = getattr(provider, "_coerce_bool")
    _enforce_strict_foreground_once = getattr(provider, "_enforce_strict_foreground_once")
    _store_active_window = getattr(provider, "_store_active_window")
    _ensure_foreground = getattr(provider, "_ensure_foreground")
    _capture_for_interaction = getattr(provider, "_capture_for_interaction")
    run_ocr_with_boxes = getattr(provider, "run_ocr_with_boxes")
    _encode_image_base64 = getattr(provider, "_encode_image_base64")
    _icon_templates_from_params = getattr(provider, "_icon_templates_from_params")
    _use_vlm = getattr(provider, "_use_vlm")
    locate_target = getattr(provider, "locate_target")
    _reject_window_match = getattr(provider, "_reject_window_match")
    get_vlm_call = getattr(provider, "get_vlm_call")
    _extract_center_from_locator = getattr(provider, "_extract_center_from_locator")
    _clamp_point = getattr(provider, "_clamp_point")
    _validate_locator_center = getattr(provider, "_validate_locator_center")
    _execute_click_strategies = getattr(provider, "_execute_click_strategies")
    InteractionStrategyError = getattr(provider, "InteractionStrategyError")
    _wait_for_ocr_targets = getattr(provider, "_wait_for_ocr_targets")
    handle_click = getattr(provider, "handle_click")

    params = step.params or {}
    targets = _extract_targets(params)
    hint = str(params.get("strategy_hint", "")).lower()
    params_summary = dict(params)
    debug = {
        "branch": "ocr_locator",
        "strategy_hint": hint,
        "params": params_summary,
        "targets": targets,
    }
    if not targets:
        return {"status": "error", "reason": "text param is required", "debug": debug}

    button = params.get("button", "left")
    attempts = params.get("attempts", 2)
    try:
        attempts = int(attempts)
    except Exception:
        attempts = 2
    attempts = max(1, min(5, attempts))
    debug["attempts"] = attempts
    verify_targets = _normalize_target_list(params.get("verify_text"))
    verify_attempts = params.get("verify_attempts", 2)
    verify_targets = _normalize_target_list(params.get("verify_text"))
    verify_attempts = params.get("verify_attempts", 2)
    preferred = _preferred_window_hint()
    preferred = preferred or (ACTIVE_WINDOW.get(None) or {})
    strict_fg = _coerce_bool(params.get("strict_foreground"), False)
    if strict_fg and not preferred:
        preferred = _get_active_window_snapshot() or {}
    pref_hwnd = preferred.get("hwnd") or preferred.get("handle")
    try:
        pref_hwnd = int(pref_hwnd) if pref_hwnd is not None else None
    except Exception:
        pref_hwnd = None
    if strict_fg and not pref_hwnd:
        return {"status": "error", "reason": "preferred_window_unavailable", "preferred": preferred}

    logs: list[str] = []
    provider_name, provider_call = get_vlm_call()
    for attempt in range(1, attempts + 1):
        logs.append(f"attempt:{attempt}")
        if strict_fg:
            ok_fg, fg_snapshot, enforcement = _enforce_strict_foreground_once(preferred, logs=logs)
            if not ok_fg:
                _store_active_window(None)
                return {
                    "status": "error",
                    "reason": "foreground_mismatch",
                    "foreground": fg_snapshot,
                    "preferred": preferred,
                    "enforcement": enforcement,
                }
        else:
            _ensure_foreground(preferred, strict_fg, logs=logs)

        screenshot_path, fg_after, capture_err = _capture_for_interaction(preferred, strict_fg)
        if capture_err:
            return {"status": "error", "reason": capture_err, "foreground": fg_after, "preferred": preferred}
        logs.append(f"screenshot:{screenshot_path}")

        try:
            _full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
            logs.append(f"ocr_boxes:{len(boxes)}")
        except Exception as exc:  # noqa: BLE001
            return f"error: ocr failed: {exc}"

        image_b64 = _encode_image_base64(Path(screenshot_path))
        icon_templates = _icon_templates_from_params(params)
        use_vlm = _use_vlm(params)
        for term in targets:
            try:
                locate_result = locate_target(
                    query=term,
                    boxes=boxes,
                    image_path=str(screenshot_path),
                    image_base64=image_b64 if (icon_templates or use_vlm) else None,
                    icon_templates=icon_templates if icon_templates else None,
                    vlm_call=provider_call if use_vlm else None,
                    vlm_provider=provider_name,
                    match_policy=MatchPolicy.CONTROL_ONLY,
                )
                _reject_window_match(locate_result, term)
            except ValueError as exc:
                return {"status": "error", "reason": str(exc)}
            if locate_result.get("status") != "success":
                continue
            if strict_fg and locate_result.get("method") == "vlm":
                return {"status": "error", "reason": "unverified_click_vlm_strict", "locator": locate_result}
            center = _extract_center_from_locator(locate_result) or locate_result.get("center") or {}
            if center:
                try:
                    with Image.open(screenshot_path) as img:
                        cx, cy = _clamp_point(float(center.get("x", 0)), float(center.get("y", 0)), img.width, img.height)
                        locate_result["center"] = {"x": cx, "y": cy}
                        center = locate_result.get("center") or center
                except Exception:
                    pass
            valid_center, center_reason = _validate_locator_center(center, locate_result)
            if not valid_center:
                return {"status": "error", "reason": center_reason, "locator": locate_result}
            if strict_fg:
                try:
                    click_result = _execute_click_strategies(locate_result, button=button)
                except InteractionStrategyError as exc:
                    return {
                        "status": "error",
                        "reason": str(exc),
                        "locator": locate_result,
                        "target_ref": getattr(exc, "target_ref", None),
                        "message": {
                            "ok": False,
                            "status": "error",
                            "method": locate_result.get("method"),
                            "pattern": None,
                            "rebind": getattr(exc, "rebind_meta", {}),
                            "reason": str(exc),
                        },
                        "log": logs,
                    }
            else:
                click_step = ActionStep(action="click", params={"x": center.get("x"), "y": center.get("y"), "button": button})
                click_result = handle_click(click_step)
            result: Dict[str, Any] = {
                "status": "clicked",
                "matched_text": locate_result.get("candidate", {}).get("text"),
                "matched_term": term,
                "center": click_result.get("center") or center,
                "bounds": locate_result.get("bounds"),
                "reason": click_result.get("reason"),
                "locator": locate_result,
                "method": click_result.get("method") or locate_result.get("method"),
                "message": click_result.get("message"),
                "log": logs,
            }
            if click_result.get("target_ref"):
                result["target_ref"] = click_result.get("target_ref")
            if verify_targets:
                verification = _wait_for_ocr_targets(verify_targets, attempts=verify_attempts, delay=0.8)
                result["verification"] = verification
                result["verified"] = bool(verification.get("success"))
            return result

        logs.append("no_match_found")

    return {
        "status": "error",
        "reason": "text_not_found",
        "targets": targets,
        "log": logs,
        "debug": debug,
    }


def handle_browser_input(step: ActionStep, *, provider: Any) -> Any:
    """
    OCR-driven field focus + typing helper for browser forms.
    """
    from pathlib import Path

    Image = getattr(provider, "Image")
    MatchPolicy = getattr(provider, "MatchPolicy")
    _extract_targets = getattr(provider, "_extract_targets")
    _normalize_target_list = getattr(provider, "_normalize_target_list")
    _coerce_bool = getattr(provider, "_coerce_bool")
    _preferred_window_hint = getattr(provider, "_preferred_window_hint")
    _get_active_window_snapshot = getattr(provider, "_get_active_window_snapshot")
    _enforce_strict_foreground_once = getattr(provider, "_enforce_strict_foreground_once")
    _store_active_window = getattr(provider, "_store_active_window")
    _ensure_foreground = getattr(provider, "_ensure_foreground")
    _capture_for_interaction = getattr(provider, "_capture_for_interaction")
    run_ocr_with_boxes = getattr(provider, "run_ocr_with_boxes")
    rank_text_candidates = getattr(provider, "rank_text_candidates")
    _locate_from_params = getattr(provider, "_locate_from_params")
    _extract_center_from_locator = getattr(provider, "_extract_center_from_locator")
    _validate_locator_center = getattr(provider, "_validate_locator_center")
    _type_with_strategies = getattr(provider, "_type_with_strategies")
    InteractionStrategyError = getattr(provider, "InteractionStrategyError")
    _wait_for_ocr_targets = getattr(provider, "_wait_for_ocr_targets")
    _clamp_point = getattr(provider, "_clamp_point")

    params = step.params or {}
    targets = _extract_targets(params)
    if not targets:
        return "error: 'text' param is required"
    value = params.get("value")
    if not isinstance(value, str):
        return "error: 'value' param is required and must be string"

    button = params.get("button", "left")
    attempts = params.get("attempts", 2)
    try:
        attempts = int(attempts)
    except Exception:
        attempts = 2
    attempts = max(1, min(5, attempts))
    verify_targets = _normalize_target_list(params.get("verify_text"))
    verify_attempts = params.get("verify_attempts", 2)
    auto_enter = _coerce_bool(params.get("auto_enter"), True)
    preferred = _preferred_window_hint()
    strict_fg = _coerce_bool(params.get("strict_foreground"), False)
    hint = str(params.get("strategy_hint", "")).lower()
    if strict_fg and not preferred:
        preferred = _get_active_window_snapshot() or {}
    pref_hwnd = preferred.get("hwnd") or preferred.get("handle")
    try:
        pref_hwnd = int(pref_hwnd) if pref_hwnd is not None else None
    except Exception:
        pref_hwnd = None
    if strict_fg and not pref_hwnd:
        return {"status": "error", "reason": "preferred_window_unavailable", "preferred": preferred}

    logs: list[str] = []
    last_candidates: list[dict[str, Any]] = []
    logs.append(f"strategy_hint:{hint}")

    for attempt in range(1, attempts + 1):
        logs.append(f"attempt:{attempt}")
        if strict_fg:
            ok_fg, fg_snapshot, enforcement = _enforce_strict_foreground_once(preferred, logs=logs)
            if not ok_fg:
                _store_active_window(None)
                return {
                    "status": "error",
                    "reason": "foreground_mismatch",
                    "foreground": fg_snapshot,
                    "preferred": preferred,
                    "enforcement": enforcement,
                }
        else:
            _ensure_foreground(preferred, strict_fg, logs=logs)

        screenshot_path, fg_after, capture_err = _capture_for_interaction(preferred, strict_fg)
        if capture_err:
            return {"status": "error", "reason": capture_err, "foreground": fg_after, "preferred": preferred}
        logs.append(f"screenshot:{screenshot_path}")

        try:
            _full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
            logs.append(f"ocr_boxes:{len(boxes)}")
        except Exception as exc:  # noqa: BLE001
            return f"error: ocr failed: {exc}"

        for term in targets:
            ranked = rank_text_candidates(term, boxes)
            if ranked:
                last_candidates.extend(ranked[:3])
            try:
                locate_result = _locate_from_params(term, params, boxes, Path(screenshot_path), match_policy=MatchPolicy.CONTROL_ONLY)
            except ValueError as exc:
                return {"status": "error", "reason": str(exc)}
            if locate_result.get("status") != "success":
                continue
            if strict_fg and locate_result.get("method") == "vlm":
                return {"status": "error", "reason": "unverified_click_vlm_strict", "locator": locate_result}
            center = _extract_center_from_locator(locate_result) or locate_result.get("center") or {}
            if center:
                try:
                    with Image.open(screenshot_path) as img:
                        cx, cy = _clamp_point(float(center.get("x", 0)), float(center.get("y", 0)), img.width, img.height)
                        locate_result["center"] = {"x": cx, "y": cy}
                        center = locate_result.get("center") or center
                except Exception:
                    pass
            valid_center, center_reason = _validate_locator_center(center, locate_result)
            if not valid_center and center:
                return {"status": "error", "reason": center_reason, "locator": locate_result}
            try:
                type_result = _type_with_strategies(value, locate_result, button=button, auto_enter=auto_enter)
            except InteractionStrategyError as exc:
                if strict_fg:
                    return {
                        "status": "error",
                        "reason": str(exc),
                        "locator": locate_result,
                        "target_ref": getattr(exc, "target_ref", None),
                        "message": {
                            "ok": False,
                            "status": "error",
                            "method": locate_result.get("method"),
                            "pattern": None,
                            "rebind": getattr(exc, "rebind_meta", {}),
                            "reason": str(exc),
                        },
                        "log": logs,
                    }
                else:
                    type_result = {"status": "success", "method": "keyboard_type", "message": "fallback_no_strict"}
            result: Dict[str, Any] = {
                "status": "typed",
                "matched_text": locate_result.get("candidate", {}).get("text"),
                "matched_term": term,
                "center": type_result.get("center") or center,
                "bounds": locate_result.get("bounds"),
                "click_result": None,
                "type_result": type_result,
                "method": type_result.get("method"),
                "locator": locate_result,
                "message": type_result.get("message"),
                "log": logs,
            }
            if type_result.get("target_ref"):
                result["target_ref"] = type_result.get("target_ref")
            if verify_targets:
                verification = _wait_for_ocr_targets(verify_targets, attempts=verify_attempts, delay=0.8)
                result["verification"] = verification
                result["verified"] = bool(verification.get("success"))
            return result

        logs.append("no_match_found")

    return {
        "status": "error",
        "reason": "text_not_found",
        "targets": targets,
        "log": logs,
        "candidates": last_candidates,
    }


__all__ = [
    "Dispatcher",
    "handle_hotkey",
    "handle_type",
    "handle_click",
    "handle_open_app",
    "handle_browser_input",
    "handle_open_url",
    "handle_wait_until",
]


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


def handle_browser_extract_text(step: ActionStep, *, provider: Any) -> Any:
    """
    Extract text from browser. Supports two modes:
    1. VLM Direct Read (strategy_hint="vlm_read"): Sends screenshot to VLM to read specific content.
    2. OCR/Locator (default): Finds a label (e.g. "Price") and extracts text near it.
    """
    from pathlib import Path

    base64_mod = getattr(provider, "base64")
    BytesIO = getattr(provider, "BytesIO")
    Image = getattr(provider, "Image")
    capture_screen = getattr(provider, "capture_screen")
    get_vlm_call = getattr(provider, "get_vlm_call")
    _encode_image_base64 = getattr(provider, "_encode_image_base64")
    _summarize_browser_extract_params = getattr(provider, "_summarize_browser_extract_params")
    _extract_targets = getattr(provider, "_extract_targets")
    run_ocr_with_boxes = getattr(provider, "run_ocr_with_boxes")
    rank_text_candidates = getattr(provider, "rank_text_candidates")
    _run_region_ocr = getattr(provider, "_run_region_ocr")
    _select_best_candidate = getattr(provider, "_select_best_candidate")
    _clamp_point = getattr(provider, "_clamp_point")
    mouse = getattr(provider, "mouse")
    pyautogui = getattr(provider, "pyautogui", None)

    params = step.params or {}
    params_summary = _summarize_browser_extract_params(params)

    hint = str(params.get("strategy_hint", "")).lower()
    if "vlm_read" in hint or ("vlm" in hint and "read" in hint):
        target_desc = None
        target_desc_source = None
        for key in ("visual_description", "target", "text", "query", "label"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                target_desc = value.strip()
                target_desc_source = key
                break
        debug = {
            "branch": "vlm_read",
            "strategy_hint": hint,
            "params": params_summary,
            "target_desc_source": target_desc_source,
        }
        if not target_desc:
            return {
                "status": "error",
                "reason": "visual_description/target/text required for vlm_read",
                "debug": debug,
            }

        try:
            screenshot_path = capture_screen()
        except Exception as exc:
            return {"status": "error", "reason": f"screenshot failed: {exc}", "debug": debug}
        debug.update({"screenshot_path": str(screenshot_path), "target_desc": target_desc})

        search_query = None
        for key in ("text", "query", "label"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                search_query = value.strip()
                break

        prompt = provider._build_vlm_read_prompt(target_desc, search_query)
        debug["prompt"] = prompt
        if search_query:
            debug["search_query"] = search_query

        provider_name, provider_call = get_vlm_call()
        if not provider_call:
            debug["vlm_provider"] = provider_name
            return {"status": "error", "reason": "No VLM provider configured (VLM_DISABLED?)", "debug": debug}

        try:
            print(f"[EXECUTOR] VLM Direct Read: {target_desc} using {provider_name}")
            prefer_top = bool(params.get("prefer_top_line")) or ("first" in target_desc.lower()) or ("第一" in target_desc)
            image_b64 = _encode_image_base64(Path(screenshot_path))
            crop_info = None
            if prefer_top and image_b64:
                try:
                    with Image.open(screenshot_path) as img:
                        width, height = img.size
                        top = int(height * 0.18)
                        bottom = int(height * 0.55)
                        if bottom - top > 20:
                            cropped = img.crop((0, top, width, bottom))
                            buf = BytesIO()
                            cropped.save(buf, format="PNG")
                            image_b64 = base64_mod.b64encode(buf.getvalue()).decode("ascii")
                            crop_info = {"top": top, "bottom": bottom, "width": width, "height": height}
                except Exception as exc:  # noqa: BLE001
                    debug["crop_error"] = str(exc)

            debug["vlm_provider"] = provider_name
            debug["image_base64_ok"] = bool(image_b64)
            if crop_info:
                debug["crop"] = crop_info

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    ],
                }
            ]

            vlm_response = provider_call(prompt, messages)
            cleaned_text = vlm_response.strip().strip('"').strip("'").strip()

            return {
                "status": "success",
                "matched_text": cleaned_text,
                "matched_term": target_desc,
                "method": "vlm_direct_read",
                "log": [f"vlm_provider:{provider_name}", f"prompt:{target_desc}"],
                "debug": debug,
            }

        except Exception as exc:
            return {"status": "error", "reason": f"vlm_read failed: {exc}", "debug": debug}

    targets = _extract_targets(params)
    debug = {
        "branch": "ocr_locator",
        "strategy_hint": hint,
        "params": params_summary,
        "targets": targets,
    }
    if not targets:
        return {"status": "error", "reason": "text param is required", "debug": debug}

    attempts = params.get("attempts", 2)
    try:
        attempts = int(attempts)
    except Exception:
        attempts = 2
    attempts = max(1, min(5, attempts))
    debug["attempts"] = attempts

    logs: list[str] = []
    last_candidates: list[dict[str, Any]] = []
    logs.append(f"strategy_hint:{hint}")

    for attempt in range(1, attempts + 1):
        logs.append(f"attempt:{attempt}")
        try:
            screenshot_path = capture_screen()
            logs.append(f"screenshot:{screenshot_path}")
        except Exception as exc:  # noqa: BLE001
            debug["attempt"] = attempt
            return {"status": "error", "reason": f"screenshot failed: {exc}", "debug": debug}

        try:
            full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
            logs.append(f"ocr_boxes:{len(boxes)}")
        except Exception as exc:  # noqa: BLE001
            debug["attempt"] = attempt
            return {"status": "error", "reason": f"ocr failed: {exc}", "debug": debug}

        best = None
        best_term = None
        for term in targets:
            ranked = rank_text_candidates(term, boxes)
            if ranked:
                last_candidates.extend(ranked[:5])
            if not ranked:
                continue
            top = ranked[0]
            if not best or top.get("high_enough") and not best.get("high_enough"):
                best = top
                best_term = term
            elif top.get("high_enough") == best.get("high_enough") and top["score"] > best["score"]:
                best = top
                best_term = term
            if top.get("high_enough"):
                break

        if best and (best.get("high_enough") or best.get("medium_enough")):
            center = best.get("center") or {}
            bounds = best.get("bounds") or {}
            debug["attempt"] = attempt
            result = {
                "status": "ok",
                "method": "ocr_locator",
                "matched_text": best.get("text"),
                "matched_term": best_term,
                "center": {"x": center.get("x"), "y": center.get("y")},
                "bounds": bounds,
                "full_text": full_text,
                "log": logs,
                "debug": debug,
            }
            if last_candidates:
                result["candidates"] = last_candidates[:10]
            return result

        logs.append("no_match_found")

    return {
        "status": "error",
        "reason": "text_not_found",
        "targets": targets,
        "log": logs,
        "candidates": last_candidates,
        "debug": debug,
    }


def handle_read_file(step: ActionStep, *, provider: Any) -> str:
    files = getattr(provider, "files")
    path = (step.params or {}).get("path")
    if not path or not isinstance(path, str):
        return "error: 'path' param is required"
    return files.read_file(step.params)


def handle_write_file(step: ActionStep, *, provider: Any) -> str:
    files = getattr(provider, "files")
    return files.write_file(step.params)


__all__ = [
    "Dispatcher",
    "handle_hotkey",
    "handle_type",
    "handle_click",
    "handle_open_app",
    "handle_browser_click",
    "handle_browser_input",
    "handle_open_url",
    "handle_browser_extract_text",
    "handle_read_file",
    "handle_wait_until",
]
