from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional, Set, Callable

from backend.executor.actions_schema import ActionStep
from backend.executor.evidence_emit import build_evidence


def _clip_text(value: Optional[str], limit: int = 300) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value[:limit]


def verify_step_outcome(
    step: ActionStep,
    status: str,
    message: Any,
    attempt: int,
    max_attempts: int,
    expected_window: Optional[Dict[str, Any]],
    before_obs: Dict[str, Any],
    after_obs: Dict[str, Any],
    work_dir: Optional[str],
    verify_mode: str = "auto",
    request_id: Optional[str] = None,
    step_index: int = 0,
    window_provider=None,
    input_actions: Optional[Set[str]] = None,
    risky_file_actions: Optional[Set[str]] = None,
    window_enumerator: Optional[Callable[[], Any]] = None,
    window_filter: Optional[Callable[[Any, Any, Any], Any]] = None,
) -> Dict[str, Any]:
    """
    Verification logic with bounded retries and structured evidence.
    """
    input_actions = input_actions or set()
    risky_file_actions = risky_file_actions or set()
    verify_mode = (verify_mode or "auto").lower()
    if verify_mode not in {"auto", "never", "always"}:
        verify_mode = "auto"

    decision = "success"
    reason = "handler_success"
    verifier = "none"
    expected: Dict[str, Any] = {}
    actual: Dict[str, Any] = {}
    action = step.action
    foreground = after_obs.get("foreground") if isinstance(after_obs, dict) else None
    text_result = None
    window_provider = window_provider

    def _retry_or_fail() -> str:
        return "retry" if attempt < max_attempts else "failed"

    params = step.params or {}

    # Classification
    ui_actions = input_actions - {"browser_extract_text"}
    read_only_actions = {"browser_extract_text", "list_windows", "get_active_window", "read_file", "open_file", "list_files"}
    file_actions = risky_file_actions | {"create_folder"}
    browser_actions = {"open_url", "browser_click", "browser_input", "browser_extract_text", "browser_wait_for_text", "browser_scroll"}

    if action == "wait_until":
        verifier = "wait_until"
        met = False
        timed_out = False
        timeout_allowed = False
        elapsed = None
        condition = None
        structural_condition = False
        if isinstance(message, dict):
            met = bool(message.get("ok"))
            status_field = str(message.get("status", "")).lower()
            timed_out = status_field == "timeout"
            timeout_allowed = bool(message.get("timeout_allowed"))
            elapsed = message.get("elapsed")
            condition = message.get("condition")
        structural_condition = (condition or "").lower() in {"window_exists", "process_exists", "foreground_matches", "title_contains"}
        expected = {
            "condition": condition,
            "target": params.get("target"),
            "timeout": params.get("timeout"),
            "poll_interval": params.get("poll_interval"),
            "stability_duration": params.get("stability_duration"),
            "stable_samples": params.get("stable_samples"),
            "require": params.get("require"),
            "allow_timeout": params.get("allow_timeout"),
        }
        actual = {"met": met, "timed_out": timed_out, "elapsed": elapsed, "timeout_allowed": timeout_allowed}
        if structural_condition:
            actual["modality_used"] = "uia"

        if met and not timed_out:
            decision = "success"
            reason = "condition_met"
        elif timed_out and timeout_allowed:
            decision = "success"
            reason = "timeout_allowed"
        else:
            decision = "failed"
            reason = "timeout" if timed_out else "condition_not_met"

        evidence = build_evidence(
            request_id,
            step_index,
            attempt,
            action,
            status,
            reason,
            "verify",
            before_obs,
            after_obs,
            foreground,
            verifier=verifier,
            expected=expected,
            actual=actual,
        )
        return {
            "decision": decision,
            "reason": reason,
            "status": status,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "verifier": verifier,
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
            "should_retry": decision == "retry",
        }

    if action == "open_app":
        verifier = "open_app"
        target = params.get("target") or ""
        title_keywords = []
        if isinstance(target, str) and target.strip():
            title_keywords.append(target.strip())
        extra_titles = params.get("title_keywords") or []
        if isinstance(extra_titles, (list, tuple)):
            title_keywords.extend([str(t) for t in extra_titles if t])
        class_keywords = params.get("class_keywords") or []
        if isinstance(class_keywords, str):
            class_keywords = [class_keywords]
        class_keywords = [str(c) for c in class_keywords if c]
        expected = {"title_keywords": title_keywords, "class_keywords": class_keywords}

        found_window: Optional[Dict[str, Any]] = None
        timeout_s = params.get("verify_timeout") or 2.0
        try:
            timeout_s = float(timeout_s)
        except Exception:
            timeout_s = 2.0
        timeout_s = max(0.1, min(timeout_s, 5.0))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and found_window is None:
            snaps = window_enumerator() if window_enumerator else []
            matches = window_filter(snaps, title_keywords, class_keywords) if window_filter else []
            if matches:
                snap = matches[0]
                found_window = {
                    "hwnd": snap.hwnd,
                    "pid": snap.pid,
                    "title": snap.title,
                    "class": snap.class_name,
                }
                break
            time.sleep(0.2)

        if found_window:
            decision = "success"
            reason = "verified"
            actual = {"window": found_window, "modality_used": "uia"}
        else:
            decision = _retry_or_fail()
            reason = "verification_retry" if decision == "retry" else "verification_failed"
            actual = {"window": None, "modality_used": "uia"}

        evidence = build_evidence(
            request_id,
            step_index,
            attempt,
            action,
            status,
            reason,
            "verify",
            before_obs,
            after_obs,
            foreground,
            verifier=verifier,
            expected=expected,
            actual=actual,
        )
        return {
            "decision": decision,
            "reason": reason,
            "status": status,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "verifier": verifier,
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
            "should_retry": decision == "retry",
        }

    if action in browser_actions:
        verifier = "browser"
        expected_url = params.get("expected_url") or params.get("url")
        expected_title = params.get("expected_title") or params.get("title")
        verify_text = params.get("verify_text") or params.get("verify_targets") or []
        if isinstance(verify_text, str):
            verify_text = [verify_text]
        verify_text = [str(t) for t in verify_text if t]
        if action == "browser_wait_for_text" and not verify_text and params.get("text"):
            verify_text = [params.get("text")]
        expected_value = params.get("value") if action == "browser_input" else None
        actual_url = None
        actual_title = None
        actual_text = None
        actual_value = None
        if isinstance(message, dict):
            actual_url = message.get("url") or message.get("current_url")
            actual_title = message.get("title") or message.get("page_title")
            actual_text = message.get("matched_text") or message.get("text") or message.get("full_text")
            actual_value = message.get("value") or message.get("text") or message.get("matched_text")
        expected = {}
        actual = {}

        def _build_browser_evidence(reason_code: str) -> Dict[str, Any]:
            return build_evidence(
                request_id,
                step_index,
                attempt,
                action,
                status,
                reason_code,
                "verify",
                before_obs,
                after_obs,
                foreground,
                verifier=verifier,
                expected=expected,
                actual=actual,
                text_result=_clip_text(actual_text),
            )

        def _retry_decision() -> str:
            return "retry" if attempt < max_attempts else "failed"

        # Common expected/actual snapshots
        if expected_url:
            expected["url_contains"] = expected_url
        if expected_title:
            expected["title_contains"] = expected_title
        if verify_text:
            expected["verify_text"] = verify_text
        if expected_value:
            expected["value_contains"] = expected_value
        if actual_url:
            actual["url"] = actual_url
        if actual_title:
            actual["title"] = actual_title
        if actual_text:
            actual["text"] = _clip_text(actual_text)
        if actual_value:
            actual["value"] = _clip_text(actual_value)

        # Action-specific verification
        if action == "open_url":
            verifier = "browser_url"
            if not expected_url and not expected_title:
                decision = "failed"
                reason = "missing_expected_verify"
            else:
                url_ok = bool(expected_url and actual_url and expected_url.lower() in actual_url.lower())
                title_ok = bool(expected_title and actual_title and expected_title.lower() in actual_title.lower())
                if url_ok or title_ok:
                    decision = "success"
                    reason = "verified"
                else:
                    if (expected_url and not actual_url) or (expected_title and not actual_title):
                        decision = _retry_decision()
                        reason = "verification_retry"
                    else:
                        decision = "failed"
                        reason = "verification_failed"
            evidence = _build_browser_evidence(reason)
            return {
                "decision": decision,
                "reason": reason,
                "status": status,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "verifier": verifier,
                "expected": expected,
                "actual": actual,
                "evidence": evidence,
                "should_retry": decision == "retry",
            }

        if action in {"browser_click", "browser_input", "browser_wait_for_text"}:
            verifier = "browser_text"
            has_expected = bool(verify_text) or bool(expected_url) or bool(expected_value)
            if not has_expected:
                decision = "failed"
                reason = "missing_expected_verify"
                evidence = _build_browser_evidence(reason)
                return {
                    "decision": decision,
                    "reason": reason,
                    "status": status,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "verifier": verifier,
                    "expected": expected,
                    "actual": actual,
                    "evidence": evidence,
                    "should_retry": False,
                }
            url_ok = bool(expected_url and actual_url and expected_url.lower() in actual_url.lower())
            text_ok = False
            if verify_text:
                if actual_text:
                    text_ok = any(t.lower() in str(actual_text).lower() for t in verify_text)
                elif actual_title:
                    text_ok = any(t.lower() in str(actual_title).lower() for t in verify_text)
            value_ok = False
            if expected_value:
                if actual_value:
                    value_ok = expected_value.lower() in str(actual_value).lower()
                elif actual_text:
                    value_ok = expected_value.lower() in str(actual_text).lower()
            if url_ok or text_ok or value_ok:
                decision = "success"
                reason = "verified"
            else:
                if (expected_url and not actual_url) or (verify_text and not actual_text and not actual_title) or (
                    expected_value and not actual_value and not actual_text
                ):
                    decision = _retry_decision()
                    reason = "verification_retry"
                else:
                    decision = "failed"
                    reason = "verification_failed"
            evidence = _build_browser_evidence(reason)
            return {
                "decision": decision,
                "reason": reason,
                "status": status,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "verifier": verifier,
                "expected": expected,
                "actual": actual,
                "evidence": evidence,
                "should_retry": decision == "retry",
            }

        if action == "browser_extract_text":
            verifier = "browser_extract"
            must_contain = params.get("text") or params.get("keywords")
            if isinstance(must_contain, str):
                must_contain = [must_contain]
            must_contain = [str(t) for t in must_contain] if must_contain else []
            if must_contain:
                expected["verify_text"] = must_contain
            if not actual_text:
                decision = _retry_decision()
                reason = "verification_retry"
            else:
                if must_contain:
                    matches = any(t.lower() in actual_text.lower() for t in must_contain)
                    if matches:
                        decision = "success"
                        reason = "verified"
                    else:
                        decision = "failed"
                        reason = "verification_failed"
                else:
                    decision = "success"
                    reason = "verified"
            evidence = _build_browser_evidence(reason)
            return {
                "decision": decision,
                "reason": reason,
                "status": status,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "verifier": verifier,
                "expected": expected,
                "actual": actual,
                "evidence": evidence,
                "should_retry": decision == "retry",
            }

        if action == "browser_scroll":
            verifier = "browser_scroll"
            if verify_text:
                if actual_text and any(t.lower() in actual_text.lower() for t in verify_text):
                    decision = "success"
                    reason = "verified"
                else:
                    decision = _retry_decision()
                    reason = "verification_retry" if decision == "retry" else "verification_failed"
            else:
                decision = "success"
                reason = "verified"
            evidence = _build_browser_evidence(reason)
            return {
                "decision": decision,
                "reason": reason,
                "status": status,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "verifier": verifier,
                "expected": expected,
                "actual": actual,
                "evidence": evidence,
                "should_retry": decision == "retry",
            }

    if status in {"error", "failed"}:
        decision = _retry_or_fail()
        reason = "handler_error"
        verifier = "none"
        evidence = build_evidence(
            request_id,
            step_index,
            attempt,
            action,
            status,
            reason,
            "verify",
            before_obs,
            after_obs,
            foreground,
            verifier=verifier,
            expected=expected,
            actual=actual,
        )
        return {
            "decision": decision,
            "reason": reason,
            "status": status,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "verifier": verifier,
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
            "should_retry": decision == "retry",
        }

    if verify_mode == "never":
        reason = "verification_skipped"
        evidence = build_evidence(
            request_id,
            step_index,
            attempt,
            action,
            status,
            reason,
            "verify",
            before_obs,
            after_obs,
            foreground,
            verifier=verifier,
            expected=expected,
            actual=actual,
        )
        return {
            "decision": decision,
            "reason": reason,
            "status": status,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "verifier": verifier,
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
            "should_retry": False,
        }

    if action in ui_actions:
        verifier = "ui_target"
        if not expected_window:
            decision = "failed"
            reason = "missing_expected"
        else:
            expected = {"target": expected_window}
            decision = "success"
            reason = "verified_target_hint"
        evidence = build_evidence(
            request_id,
            step_index,
            attempt,
            action,
            status,
            reason,
            "verify",
            before_obs,
            after_obs,
            foreground,
            verifier=verifier,
            expected=expected,
            actual=actual,
            focus_expected=expected_window,
            focus_actual=foreground,
        )
        return {
            "decision": decision,
            "reason": reason,
            "status": status,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "verifier": verifier,
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
            "should_retry": decision == "retry",
        }

    if action in file_actions:
        verifier = "file_state"
        path = params.get("path") or params.get("source")
        dest = params.get("destination_dir") or params.get("destination")
        expected["path"] = path
        expected["destination"] = dest
        try:
            if action == "delete_file":
                actual["exists"] = Path(path).exists() if path else None
                ok = path and not Path(path).exists()
            elif action in {"move_file", "copy_file"} and dest:
                target = Path(dest) / Path(path or "").name if path else None
                actual["exists_at_dest"] = target.exists() if target else None
                ok = bool(target and target.exists())
            elif action == "rename_file" and dest:
                target = Path(path).with_name(dest) if path else None
                actual["exists_new_name"] = target.exists() if target else None
                ok = bool(target and target.exists())
            elif action == "write_file":
                actual["exists"] = Path(path).exists() if path else None
                ok = bool(path and Path(path).exists())
            elif action == "create_folder":
                actual["exists"] = Path(path).exists() if path else None
                ok = bool(path and Path(path).exists())
            else:
                ok = True
            if ok:
                decision = "success"
                reason = "verified_file_state"
            else:
                decision = _retry_or_fail()
                reason = "verification_failed"
        except Exception as exc:  # noqa: BLE001
            decision = "failed"
            reason = f"verification_error:{exc}"
        evidence = build_evidence(
            request_id,
            step_index,
            attempt,
            action,
            status,
            reason,
            "verify",
            before_obs,
            after_obs,
            foreground,
            verifier=verifier,
            expected=expected,
            actual=actual,
            file_check=actual,
        )
        return {
            "decision": decision,
            "reason": reason,
            "status": status,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "verifier": verifier,
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
            "should_retry": decision == "retry",
        }

    if status in {"error", "failed"}:
        decision = _retry_or_fail()
        reason = "handler_error"
        verifier = "none"
        evidence = build_evidence(
            request_id,
            step_index,
            attempt,
            action,
            status,
            reason,
            "verify",
            before_obs,
            after_obs,
            foreground,
            verifier=verifier,
            expected=expected,
            actual=actual,
        )
        return {
            "decision": decision,
            "reason": reason,
            "status": status,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "verifier": verifier,
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
            "should_retry": decision == "retry",
        }

    if action in read_only_actions:
        verifier = "read_only"
        decision = "success" if status not in {"error", "failed"} else "failed"
        reason = "verified" if decision == "success" else "verification_failed"
        if isinstance(message, dict) and "text" in message:
            text_result = message.get("text")
        evidence = build_evidence(
            request_id,
            step_index,
            attempt,
            action,
            status,
            reason,
            "verify",
            before_obs,
            after_obs,
            foreground,
            verifier=verifier,
            expected=expected,
            actual=actual,
            text_result=_clip_text(text_result),
        )
        return {
            "decision": decision,
            "reason": reason,
            "status": status,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "verifier": verifier,
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
            "should_retry": False,
        }

    verifier = "generic"
    decision = "success" if status not in {"error", "failed"} else "failed"
    reason = "verified" if decision == "success" else "verification_failed"
    evidence = build_evidence(
        request_id,
        step_index,
        attempt,
        action,
        status,
        reason,
        "verify",
        before_obs,
        after_obs,
        foreground,
        verifier=verifier,
        expected=expected,
        actual=actual,
    )
    return {
        "decision": decision,
        "reason": reason,
        "status": status,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "verifier": verifier,
        "expected": expected,
        "actual": actual,
        "evidence": evidence,
        "should_retry": False,
    }


__all__ = ["verify_step_outcome", "_clip_text"]
