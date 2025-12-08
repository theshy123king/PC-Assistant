from backend.executor.actions_schema import ActionPlan, ActionStep
from backend.executor.executor import VLM_DISABLED, run_steps


def test_vlm_flag_is_scoped_per_run():
    """
    Ensure VLM disable flag does not leak across run_steps calls.
    """
    baseline = VLM_DISABLED.get()
    plan = ActionPlan(task="noop", steps=[ActionStep(action="wait", params={"seconds": 0})])

    run_steps(plan, capture_observations=False, disable_vlm=True)
    assert VLM_DISABLED.get() == baseline

    run_steps(plan, capture_observations=False, disable_vlm=False)
    assert VLM_DISABLED.get() == baseline
