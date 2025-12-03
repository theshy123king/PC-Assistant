"""
Structured task context shared across planning and execution.

Captures the user instruction, generated plan, step results, screenshot/ocr
metadata, and any errors for downstream analysis or replanning.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.executor.actions_schema import ActionPlan


class TaskContext:
    def __init__(
        self,
        user_instruction: Optional[str] = None,
        screenshot_meta: Optional[Dict[str, Any]] = None,
        ocr_text: Optional[str] = None,
        feedback_config: Optional[Dict[str, Any]] = None,
        max_replans: Optional[int] = None,
        work_dir: Optional[str] = None,
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
        }


__all__ = ["TaskContext"]
