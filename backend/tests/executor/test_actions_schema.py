from backend.executor.actions_schema import validate_action_plan


def test_validate_action_plan_accepts_browser_click():
    plan = {
        "task": "click a browser button",
        "steps": [
            {
                "action": "browser_click",
                "params": {"text": "Search", "variants": ["搜索", "Find"]},
            }
        ],
    }

    validated = validate_action_plan(plan)

    assert validated.steps[0].action == "browser_click"
    assert validated.steps[0].params["text"] == "Search"


def test_validate_action_plan_accepts_browser_input():
    plan = {
        "task": "type into field",
        "steps": [
            {
                "action": "browser_input",
                "params": {"text": "Email", "variants": ["邮箱"], "value": "user@example.com"},
            }
        ],
    }

    validated = validate_action_plan(plan)

    assert validated.steps[0].action == "browser_input"
    assert validated.steps[0].params["value"] == "user@example.com"


def test_validate_action_plan_accepts_browser_extract_text():
    plan = {
        "task": "read status",
        "steps": [
            {
                "action": "browser_extract_text",
                "params": {"text": "Status", "variants": ["状态"]},
            }
        ],
    }

    validated = validate_action_plan(plan)

    assert validated.steps[0].action == "browser_extract_text"
    assert validated.steps[0].params["text"] == "Status"


def test_validate_action_plan_normalizes_copy_file_destination_alias():
    plan = {
        "task": "copy a file",
        "steps": [
            {
                "action": "copy_file",
                "params": {"source": "F:/data.txt", "destination": "F:/backup"},
            }
        ],
    }

    validated = validate_action_plan(plan)

    assert validated.steps[0].params["destination_dir"] == "F:/backup"
