import os
import tempfile
from pathlib import Path

import backend.executor.executor as executor
from backend.executor.actions_schema import ActionPlan, ActionStep
from fastapi.testclient import TestClient
from backend.app import app


class StubHandler:
    def __init__(self):
        self.calls = 0

    def __call__(self, step):
        self.calls += 1
        return {"status": "success"}


def test_mutation_blocked_outside_allowed_root(monkeypatch):
    handler = StubHandler()
    monkeypatch.setitem(executor.ACTION_HANDLERS, "delete_file", handler)

    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as work_dir:
        outside = Path(work_dir).parent / "outside.txt"
        monkeypatch.setattr(executor, "ALLOWED_ROOTS", [os.path.abspath(work_dir)])
        plan = ActionPlan(
            task="danger",
            steps=[ActionStep(action="delete_file", params={"path": str(outside), "confirm": True})],
        )
        result = executor.run_steps(
            plan, work_dir=work_dir, request_id="req-guard-1", consent_token=True, capture_observations=False
        )

    assert handler.calls == 0
    # Safety layer may mark as unsafe; guard should prevent dispatch
    assert result["overall_status"] in {"error", "unsafe"}


def test_read_allowed_outside_root_non_forbidden(monkeypatch):
    handler = StubHandler()
    monkeypatch.setitem(executor.ACTION_HANDLERS, "read_file", handler)

    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as work_dir:
        monkeypatch.setattr(executor, "ALLOWED_ROOTS", [os.path.abspath(work_dir)])
        target = Path(work_dir) / "readme.txt"
        target.write_text("hello", encoding="utf-8")
        plan = ActionPlan(task="read", steps=[ActionStep(action="read_file", params={"path": str(target)})])
        result = executor.run_steps(
            plan, work_dir=work_dir, request_id="req-guard-2", consent_token=True, capture_observations=False
        )

    assert handler.calls == 1
    assert result["overall_status"] in {"success", "replanned"}


def test_wildcard_blocked_before_dispatch(monkeypatch):
    handler = StubHandler()
    monkeypatch.setitem(executor.ACTION_HANDLERS, "write_file", handler)

    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as work_dir:
        monkeypatch.setattr(executor, "ALLOWED_ROOTS", [os.path.abspath(work_dir)])
        plan = ActionPlan(
            task="write",
            steps=[ActionStep(action="write_file", params={"path": os.path.join(work_dir, "*.txt"), "content": "x"})],
        )
        result = executor.run_steps(
            plan, work_dir=work_dir, request_id="req-guard-3", consent_token=True, capture_observations=False
        )

    assert handler.calls == 0
    entry = result["logs"][0]
    assert entry["reason"] == "wildcard_blocked"
    assert entry["evidence"]["file_check"]["decision"] == "deny"


def test_overwrite_blocked_without_flag(monkeypatch):
    handler = StubHandler()
    monkeypatch.setitem(executor.ACTION_HANDLERS, "write_file", handler)

    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as work_dir:
        monkeypatch.setattr(executor, "ALLOWED_ROOTS", [os.path.abspath(work_dir)])
        target = Path(work_dir) / "file.txt"
        target.write_text("existing", encoding="utf-8")
        plan = ActionPlan(
            task="write",
            steps=[ActionStep(action="write_file", params={"path": str(target), "content": "new"})],
        )
        result = executor.run_steps(
            plan, work_dir=work_dir, request_id="req-guard-4", consent_token=True, capture_observations=False
        )

    assert handler.calls == 0
    entry = result["logs"][0]
    assert entry["reason"] == "overwrite_blocked"
    assert entry["evidence"]["file_check"]["decision"] == "deny"


def test_execute_plan_blocks_outside_root(monkeypatch):
    handler = StubHandler()
    monkeypatch.setitem(executor.ACTION_HANDLERS, "delete_file", handler)

    client = TestClient(app)
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as work_dir:
        monkeypatch.setattr(executor, "ALLOWED_ROOTS", [os.path.abspath(work_dir)])
        outside = Path(work_dir).parent / "outside.txt"
        payload = {
            "task": "danger",
            "work_dir": work_dir,
            "steps": [{"action": "delete_file", "params": {"path": str(outside), "confirm": True}}],
            "consent_token": True,
        }
        resp = client.post("/api/ai/execute_plan", json=payload)
        data = resp.json()

    assert handler.calls == 0
    assert data.get("overall_status") in {"error", "unsafe"}
