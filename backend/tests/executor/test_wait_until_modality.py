import asyncio
from pathlib import Path

import backend.executor.executor as ex
from backend.executor.actions_schema import ActionPlan, ActionStep


def test_structural_wait_until_no_ocr(monkeypatch):
    called_ocr = {"count": 0}

    def fake_run_ocr(_path):
        called_ocr["count"] += 1
        raise AssertionError("OCR should not be called for structural wait")

    monkeypatch.setattr(ex, "run_ocr", fake_run_ocr)
    monkeypatch.setattr(ex, "find_element", lambda *args, **kwargs: None)
    monkeypatch.setitem(ex.ACTION_HANDLERS, "wait_until", ex.handle_wait_until)

    plan = ActionPlan(
        task="wait",
        steps=[
            ActionStep(
                action="wait_until",
                params={
                    "condition": "window_exists",
                    "target": "Demo",
                    "timeout": 0.01,
                    "poll_interval": 0.005,
                    "capture_ocr": True,  # force request to ensure override disables it
                },
            )
        ],
    )
    result = ex.run_steps(plan, work_dir=str(Path.cwd()), request_id="req-mod-1", consent_token=True)

    assert called_ocr["count"] == 0
    entry = result["logs"][0]
    assert entry["reason"] == "timeout"
    assert entry["status"] == "error"
    evidence = entry["attempts"][0]["verification"]["evidence"]
    assert evidence["actual"].get("modality_used") == "uia"


def test_open_app_verification_modality(monkeypatch):
    monkeypatch.setitem(ex.ACTION_HANDLERS, "open_app", lambda step: {"status": "success"})
    monkeypatch.setattr(ex, "_enum_top_windows", lambda: [])

    plan = ActionPlan(
        task="open",
        steps=[ActionStep(action="open_app", params={"target": "Demo", "verify_timeout": 0.1})],
    )
    result = ex.run_steps(plan, work_dir=str(Path.cwd()), request_id="req-mod-2", consent_token=True)

    entry = result["logs"][0]
    ver = entry["attempts"][0]["verification"]
    assert ver["verifier"] == "open_app"
    assert ver["evidence"]["actual"].get("modality_used") == "uia"
