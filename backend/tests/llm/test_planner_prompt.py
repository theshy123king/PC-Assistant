from backend.llm.planner_prompt import format_prompt


def test_format_prompt_includes_vision_messages_when_image_supplied():
    bundle = format_prompt("do thing", ocr_text="text", image_base64="abc123")

    assert bundle.prompt_text
    assert bundle.vision_messages is not None
    vision_user = bundle.vision_messages[1]["content"]
    assert any(part.get("type") == "image_url" for part in vision_user if isinstance(part, dict))


def test_format_prompt_fallback_text_only():
    bundle = format_prompt("do thing", ocr_text="text", image_base64=None)

    assert bundle.vision_messages is None
    assert bundle.messages[0]["role"] == "system"


def test_format_prompt_includes_recent_steps_and_failure_info():
    bundle = format_prompt(
        "repair task",
        ocr_text="",
        recent_steps="step1 -> error",
        failure_info="click failed",
    )

    assert "step1 -> error" in bundle.prompt_text
    assert "click failed" in bundle.prompt_text
