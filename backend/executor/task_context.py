"""
Structured task context shared across planning and execution.

Captures the user instruction, generated plan, step results, screenshot/ocr
metadata, and any errors for downstream analysis or replanning.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Dict, List, Optional
from typing import TYPE_CHECKING

import uiautomation as auto

from backend.executor.actions_schema import ActionPlan

if TYPE_CHECKING:
    from backend.observability.store import EvidenceStore

class TaskContext:
    def __init__(
        self,
        user_instruction: Optional[str] = None,
        screenshot_meta: Optional[Dict[str, Any]] = None,
        ocr_text: Optional[str] = None,
        feedback_config: Optional[Dict[str, Any]] = None,
        max_replans: Optional[int] = None,
        work_dir: Optional[str] = None,
        request_id: Optional[str] = None,
        evidence_store: Optional["EvidenceStore"] = None,
    ) -> None:
        self.user_instruction = user_instruction or ""
        self.action_plan: Optional[Dict[str, Any]] = None
        self.step_results: List[Dict[str, Any]] = []
        self.screenshot_meta = screenshot_meta or {}
        self.ocr_text = ocr_text or ""
        self.target_resolution = {
            "width": (screenshot_meta or {}).get("width"),
            "height": (screenshot_meta or {}).get("height"),
        }
        self.errors: List[Dict[str, Any]] = []
        self.prompt_text: Optional[str] = None
        self.raw_reply: Optional[str] = None
        self.feedback_config: Dict[str, Any] = feedback_config or {}
        self.replan_count: int = 0
        self.max_replans: Optional[int] = max_replans
        self.replan_history: List[Dict[str, Any]] = []
        self.summary: Optional[Dict[str, Any]] = None
        self.work_dir: Optional[str] = work_dir
        self.active_window: Optional[Dict[str, Any]] = None
        self.request_id: Optional[str] = request_id
        self.evidence_store: Optional["EvidenceStore"] = evidence_store

    def record_plan(self, plan: ActionPlan | Dict[str, Any]) -> None:
        try:
            self.action_plan = plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)
        except Exception:
            self.action_plan = {"error": "failed to serialize plan"}

    def record_step_result(self, entry: Dict[str, Any]) -> None:
        self.step_results.append(entry)
        if entry.get("status") == "error":
            self.errors.append({"step_index": entry.get("step_index"), "message": entry.get("message")})

    def add_error(self, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload = {"message": message}
        if extra:
            payload.update(extra)
        self.errors.append(payload)

    def set_prompt_text(self, prompt_text: str) -> None:
        self.prompt_text = prompt_text

    def set_raw_reply(self, raw: str) -> None:
        self.raw_reply = raw

    def set_feedback_config(self, config: Dict[str, Any]) -> None:
        try:
            self.feedback_config = dict(config)
        except Exception:
            self.feedback_config = {}

    def set_max_replans(self, max_replans: Optional[int]) -> None:
        if max_replans is None:
            return
        try:
            self.max_replans = int(max_replans)
        except Exception:
            self.max_replans = max_replans

    def record_replan(self, payload: Dict[str, Any]) -> None:
        try:
            self.replan_history.append(dict(payload))
        except Exception:
            self.replan_history.append({"error": "failed to serialize replan payload"})
        self.replan_count = len(self.replan_history)

    def set_summary(self, summary: Dict[str, Any]) -> None:
        try:
            self.summary = dict(summary)
        except Exception:
            self.summary = {"error": "failed to set summary"}

    def get_ui_fingerprint(self, lite_only: bool = False) -> str:
        """
        Compute a lightweight UI state fingerprint (foreground window + focused control).
        When lite_only is False, enrich with a snapshot hash of up to 50 visible children.
        Always returns a string hash; on error returns a random token.
        """
        try:
            with auto.UIAutomationInitializerInThread(debug=False):
                fg = auto.GetForegroundControl()
                focused = auto.GetFocusedControl()
                parts: List[str] = []
                if fg:
                    try:
                        parts.append(str(getattr(fg, "NativeWindowHandle", "")))
                        parts.append(str(getattr(fg, "Name", "") or ""))
                        parts.append(str(getattr(fg, "ProcessId", "") or ""))
                    except Exception:
                        pass
                if focused:
                    try:
                        rid = getattr(focused, "RuntimeId", None)
                        if rid:
                            parts.append(str(rid))
                    except Exception:
                        pass
                lite_hash = hashlib.md5("|".join(parts).encode("utf-8", "ignore")).hexdigest()
                if lite_only:
                    return lite_hash

                snapshot: List[str] = []
                try:
                    top = fg.GetTopLevelControl() if fg else None
                except Exception:
                    top = None
                if top:
                    try:
                        children = top.GetChildren()
                    except Exception:
                        children = []
                    count = 0
                    for child in children:
                        if count >= 50:
                            break
                        try:
                            if getattr(child, "IsOffscreen", True):
                                continue
                            ctype = str(getattr(child, "ControlTypeName", "") or "")
                            name = str(getattr(child, "Name", "") or "")
                            rect = getattr(child, "BoundingRectangle", None)
                            bounds = ""
                            if rect:
                                bounds = f"{rect.left},{rect.top},{rect.right},{rect.bottom}"
                            snapshot.append(f"{ctype}|{name}|{bounds}")
                            count += 1
                        except Exception:
                            continue
                full_hash = hashlib.md5((lite_hash + "|" + ";".join(snapshot)).encode("utf-8", "ignore")).hexdigest()
                return full_hash
        except Exception:
            return f"uia_error_{uuid.uuid4().hex}"

    def emit_event(
        self,
        type: str,
        payload: Dict[str, Any],
        *,
        step_index: Optional[int] = None,
        attempt: Optional[int] = None,
        artifact_bytes: Optional[bytes] = None,
        artifact_kind: Optional[str] = None,
        artifact_mime: Optional[str] = None,
        artifact_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Lightweight helper to emit evidence events without blocking.
        """
        if not self.evidence_store or not self.request_id:
            return
        try:
            self.evidence_store.emit_sync(
                self.request_id,
                type,
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_instruction": self.user_instruction,
            "action_plan": self.action_plan,
            "step_results": self.step_results,
            "screenshot_meta": self.screenshot_meta,
            "target_resolution": self.target_resolution,
            "ocr_text": self.ocr_text,
            "errors": self.errors,
            "prompt_text": self.prompt_text,
            "raw_reply": self.raw_reply,
            "feedback_config": self.feedback_config,
            "replan_count": self.replan_count,
            "max_replans": self.max_replans,
            "replan_history": self.replan_history,
            "summary": self.summary,
            "work_dir": self.work_dir,
            "active_window": self.active_window,
        }


__all__ = ["TaskContext"]
