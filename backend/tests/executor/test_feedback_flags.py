from backend.executor.actions_schema import ActionPlan, ActionStep
from backend.executor.executor import run_steps


def test_per_step_capture_flags_disable_observation():
    plan = ActionPlan(
        task="noop",
        steps=[
            ActionStep(
                action="wait",
                params={
                    "seconds": 0,
                    "capture_before": False,
                    "capture_after": False,
                    "run_ocr_after": True,
                    "verify_mode": "never",
                    "allow_vlm": False,
                },
            )
        ],
    )

    result = run_steps(
        plan,
        capture_observations=False,
        force_capture=False,
        force_ocr=False,
        allow_replan=False,
        max_retries=0,
    )

    log = result["logs"][0]
    attempt = log["attempts"][0]
    assert attempt["observation"]["before"]["capture_enabled"] is False
    assert attempt["observation"]["after"]["capture_enabled"] is False
    assert log["feedback"]["verify_mode"] == "never"
    assert log["feedback"]["allow_vlm"] is False
