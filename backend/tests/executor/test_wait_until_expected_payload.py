from pathlib import Path

import backend.executor.executor as ex
from backend.executor.actions_schema import ActionPlan, ActionStep


def test_wait_until_expected_payload_complete(monkeypatch):
    def fake_wait(step):
        return {
            "status": "timeout",
            "ok": False,
            "condition": "window_exists",
            "elapsed": 0.01,
            "timeout_allowed": False,
        }

    monkeypatch.setitem(ex.ACTION_HANDLERS, "wait_until", fake_wait)

    plan = ActionPlan(
        task="wait",
        steps=[
            ActionStep(
                action="wait_until",
                params={
                    "condition": "window_exists",
                    "target": "DemoTarget",
                    "timeout": 0.1,
                    "poll_interval": 0.01,
                    "stability_duration": 0.5,
                    "stable_samples": 2,
                    "allow_timeout": False,
                },
            )
        ],
    )

    result = ex.run_steps(plan, work_dir=str(Path.cwd()), request_id="req-wait-expected", consent_token=True)
    evidence = result["logs"][0]["attempts"][0]["verification"]["evidence"]
    expected = evidence["expected"]
    assert expected["condition"] == "window_exists"
    assert expected["target"] == "DemoTarget"
    assert expected["timeout"] == 0.1
    assert expected["poll_interval"] == 0.01
    assert expected["stable_samples"] == 2
    assert expected["allow_timeout"] is False
