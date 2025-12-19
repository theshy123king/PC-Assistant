from __future__ import annotations

from typing import Any, Dict, Optional

from backend.utils.time_utils import now_iso_utc


def build_evidence(
    request_id: Optional[str],
    step_index: int,
    attempt: int,
    action: str,
    status: str,
    reason: str,
    capture_phase: str,
    before_obs: Optional[Dict[str, Any]] = None,
    after_obs: Optional[Dict[str, Any]] = None,
    foreground: Optional[Dict[str, Any]] = None,
    verifier: Optional[str] = None,
    expected: Optional[Dict[str, Any]] = None,
    actual: Optional[Dict[str, Any]] = None,
    risk: Optional[Dict[str, Any]] = None,
    focus_expected: Optional[Dict[str, Any]] = None,
    focus_actual: Optional[Dict[str, Any]] = None,
    file_check: Optional[Dict[str, Any]] = None,
    text_result: Optional[Any] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    return {
        "request_id": request_id,
        "step_index": step_index,
        "attempt": attempt,
        "action": action,
        "status": status,
        "reason": reason,
        "timestamp": now_iso_utc(),
        "capture_phase": capture_phase,
        "before_obs_ref": before_obs,
        "after_obs_ref": after_obs,
        "foreground": foreground,
        "verifier": verifier,
        "expected": expected,
        "actual": actual,
        "risk": risk,
        "focus_expected": focus_expected,
        "focus_actual": focus_actual,
        "file_check": file_check,
        "text_result": text_result,
        "dry_run": dry_run,
    }


def emit_context_event(
    context,
    event_type: str,
    payload: Dict[str, Any],
    *,
    step_index: Optional[int] = None,
    attempt: Optional[int] = None,
    artifact_bytes: Optional[bytes] = None,
    artifact_kind: Optional[str] = None,
    artifact_mime: Optional[str] = None,
    artifact_meta: Optional[Dict[str, Any]] = None,
) -> None:
    if not context or not hasattr(context, "emit_event"):
        return
    try:
        context.emit_event(
            event_type,
            payload,
            step_index=step_index,
            attempt=attempt,
            artifact_bytes=artifact_bytes,
            artifact_kind=artifact_kind,
            artifact_mime=artifact_mime,
            artifact_meta=artifact_meta,
        )
    except Exception:
        return


__all__ = ["build_evidence", "emit_context_event"]
