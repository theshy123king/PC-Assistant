from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Iterable, List

# Structured event logger configured in logging_setup.
event_logger = logging.getLogger("backend.events")


def generate_request_id() -> str:
    """Return a short, collision-resistant request id."""
    return uuid.uuid4().hex


def _truncate(value: str, max_len: int = 2000) -> str:
    if len(value) <= max_len:
        return value
    return f"{value[:max_len]}...<truncated {len(value) - max_len} chars>"


def _sanitize_obj(obj: Any, max_len: int = 2000, keep_full: Iterable[str] | None = None) -> Any:
    keep_full = set(keep_full or [])
    if isinstance(obj, dict):
        sanitized: Dict[str, Any] = {}
        for key, val in obj.items():
            if key in {"screenshot_base64", "image_base64"}:
                sanitized[key] = "<redacted:image>"
                continue
            if key == "raw_reply":
                sanitized[key] = _truncate(str(val), max_len)
                continue
            if key in keep_full:
                sanitized[key] = val
                continue
            sanitized[key] = _sanitize_obj(val, max_len=max_len, keep_full=keep_full)
        return sanitized
    if isinstance(obj, list):
        return [_sanitize_obj(item, max_len=max_len, keep_full=keep_full) for item in obj[:50]]
    if isinstance(obj, str):
        return _truncate(obj, max_len=max_len)
    return obj


def sanitize_payload(payload: Dict[str, Any], keep_full: Iterable[str] | None = None) -> Dict[str, Any]:
    """Return a sanitized shallow copy safe for logging."""
    try:
        return dict(_sanitize_obj(payload, keep_full=keep_full or []))
    except Exception:
        return {"error": "failed_to_sanitize"}


def summarize_plan(plan: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(plan, dict):
        return {"present": False}
    steps: List[Dict[str, Any]] = []
    for step in plan.get("steps", [])[:15]:
        action = step.get("action")
        params = step.get("params") or {}
        steps.append({"action": action, "params_keys": sorted(params.keys())})
    return {
        "present": True,
        "task": plan.get("task"),
        "total_steps": len(plan.get("steps", []) if isinstance(plan.get("steps"), list) else []),
        "steps_preview": steps,
    }


def summarize_execution(execution: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(execution, dict):
        return {"present": False}
    logs = execution.get("logs") or []
    errors = [log for log in logs if isinstance(log, dict) and log.get("status") in {"error", "unsafe"}]
    last_error = errors[-1] if errors else None
    return {
        "present": True,
        "overall_status": execution.get("overall_status"),
        "step_count": len(logs) if isinstance(logs, list) else None,
        "errors": len(errors),
        "last_error": _sanitize_obj(last_error, max_len=500) if last_error else None,
        "replan_count": (execution.get("context") or {}).get("replan_count"),
    }


def log_event(event: str, request_id: str, payload: Dict[str, Any] | None = None) -> None:
    """Log a structured event as JSON; never raise."""
    body = {"event": event, "request_id": request_id}
    if payload:
        body.update(sanitize_payload(payload, keep_full={"user_text"}))
    try:
        event_logger.info(json.dumps(body, ensure_ascii=True, default=str))
    except Exception:
        # Fallback to best-effort string logging.
        event_logger.info(f"{event} {request_id} {body}")
