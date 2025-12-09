import base64
import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

import setup_test_environment
from backend.app import app

DATA_FILE = Path(__file__).parent / "user_view_tests.json"
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
WORKSPACE_DIR = Path(__file__).parent / "test_data" / "workspace"
TEST_BACKEND_PORT = "5015"


def _substitute_placeholders(obj: Any, mapping: Dict[str, str]) -> Any:
    if isinstance(obj, str):
        value = obj
        for key, val in mapping.items():
            value = value.replace(f"{{{{{key}}}}}", val)
        return value
    if isinstance(obj, list):
        return [_substitute_placeholders(item, mapping) for item in obj]
    if isinstance(obj, dict):
        return {k: _substitute_placeholders(v, mapping) for k, v in obj.items()}
    return obj


def _load_cases(mapping: Dict[str, str]) -> list[dict]:
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return [_substitute_placeholders(case, mapping) for case in raw]


def _encode_screenshot(name: str) -> str:
    path = Path(name)
    if not path.is_absolute():
        path = SCREENSHOTS_DIR / name
    data = path.read_bytes()
    return base64.b64encode(data).decode("ascii")


def _normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "task": plan.get("task"),
        "steps": [
            {"action": step.get("action"), "params": step.get("params", {})} for step in plan.get("steps", [])
        ],
    }


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        if "\\" in value or "/" in value:
            try:
                return str(Path(value).resolve())
            except Exception:
                return value
        return value
    return value


def _params_subset(actual: Dict[str, Any], expected: Dict[str, Any]) -> bool:
    for key, val in expected.items():
        if key not in actual:
            return False
        if _normalize_value(actual[key]) != _normalize_value(val):
            return False
    return True


def _plans_match(actual: Dict[str, Any], expected: Dict[str, Any]) -> bool:
    a_steps = _normalize_plan(actual)["steps"]
    e_steps = _normalize_plan(expected)["steps"]
    if len(a_steps) != len(e_steps):
        return False
    for a_step, e_step in zip(a_steps, e_steps):
        if a_step.get("action") != e_step.get("action"):
            return False
        a_params = {k: v for k, v in (a_step.get("params") or {}).items() if v is not None}
        e_params = {k: v for k, v in (e_step.get("params") or {}).items() if v is not None}
        # Ignore default left-button if not asserted.
        if "button" in a_params and a_params.get("button") == "left" and "button" not in e_params:
            a_params.pop("button")
        if not _params_subset(a_params, e_params):
            return False
    return True


def _validate_execution(result: Dict[str, Any], rules: Dict[str, Any]) -> None:
    if not rules:
        return
    if "overall_status" in rules:
        assert result.get("overall_status") == rules["overall_status"]

    def _assert_exists(path_str: str) -> Path:
        path = Path(path_str)
        assert path.exists(), f"Expected path to exist: {path}"
        return path

    if "destination_exists" in rules:
        _assert_exists(rules["destination_exists"])
    if "source_exists" in rules:
        _assert_exists(rules["source_exists"])
    if "file_contains" in rules:
        target_path = (
            Path(rules.get("destination_exists") or rules.get("source_exists") or rules.get("path", ""))
        )
        assert target_path.exists(), f"File for content check missing: {target_path}"
        content = target_path.read_text(encoding="utf-8")
        assert rules["file_contains"] in content, f"Expected '{rules['file_contains']}' in {target_path}"


def _reset_workspace() -> None:
    """Ensure file-based tests run on a clean workspace."""
    if not WORKSPACE_DIR.exists():
        return
    for folder in ["backup", "archive", "temp"]:
        folder_path = WORKSPACE_DIR / folder
        folder_path.mkdir(parents=True, exist_ok=True)
        for item in folder_path.iterdir():
            try:
                if item.is_file() or item.is_symlink():
                    item.unlink(missing_ok=True)
                elif item.is_dir():
                    for nested in item.iterdir():
                        if nested.is_file() or nested.is_symlink():
                            nested.unlink(missing_ok=True)
                    nested_dir = item
                    nested_dir.rmdir()
            except Exception:
                continue
    # Recreate baseline files
    notes = WORKSPACE_DIR / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "readme.txt").write_text("hello, this is a test file.", encoding="utf-8")
    temp = WORKSPACE_DIR / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    (temp / "move_me.txt").write_text("move me to archive", encoding="utf-8")
    for fname in ["output.txt", "test_e2e.txt"]:
        target = WORKSPACE_DIR / fname
        if target.exists():
            target.unlink()


@pytest.fixture(scope="session", autouse=True)
def prepare_env() -> Dict[str, str]:
    setup_test_environment.main()
    os.environ["EXECUTOR_ALLOWED_ROOTS"] = str(Path.cwd())
    os.environ["EXECUTOR_TEST_MODE"] = "1"
    os.environ.setdefault("PC_ASSISTANT_TEST_PORT", TEST_BACKEND_PORT)
    return {"workspace": str(WORKSPACE_DIR)}


@pytest.mark.parametrize("case", _load_cases({"workspace": str(WORKSPACE_DIR)}), ids=lambda c: c["id"])
def test_user_view_case(case: Dict[str, Any], prepare_env: Dict[str, str]) -> None:
    _reset_workspace()
    client = TestClient(app)

    payload: Dict[str, Any] = {
        "provider": case.get("provider", "test"),
        "text": case["input_text"],
    }
    if case.get("screenshot"):
        payload["screenshot_base64"] = _encode_screenshot(case["screenshot"])

    resp = client.post("/api/ai/plan", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    if "expected_error" in case:
        assert data["status"] == "error"
        assert case["expected_error"]["type"] in data.get("message", ""), data
        return

    assert data["status"] == "success", data
    assert _plans_match(data["plan"], case["expected_plan"])

    exec_payload = data["plan"]
    exec_resp = client.post("/api/ai/execute", json=exec_payload)
    assert exec_resp.status_code == 200, exec_resp.text
    exec_result = exec_resp.json()

    _validate_execution(exec_result, case.get("execution_validation", {}))
