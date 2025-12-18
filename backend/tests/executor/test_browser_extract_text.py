from pathlib import Path

from PIL import Image

from backend.executor.actions_schema import ActionStep
import backend.executor.executor as executor
from backend.vision.ocr import OcrBox


def test_handle_browser_extract_text_returns_match(monkeypatch, tmp_path):
    screenshot = tmp_path / "shot.png"
    Image.new("RGB", (300, 150), color="white").save(screenshot)

    monkeypatch.setattr(executor, "capture_screen", lambda: Path(screenshot))
    monkeypatch.setattr(
        executor,
        "run_ocr_with_boxes",
        lambda path: ("Status Ready", [OcrBox(text="Status Ready", x=10, y=20, width=80, height=20, conf=95.0)]),
    )

    result = executor.handle_browser_extract_text(
        ActionStep(
            action="browser_extract_text",
            params={"text": "Status", "variants": ["状态"]},
        )
    )

    assert result["status"] == "ok"
    assert result["matched_text"] == "Status Ready"


def test_handle_browser_extract_text_vlm_read_prefers_visual_description(monkeypatch, tmp_path):
    screenshot = tmp_path / "shot.png"
    Image.new("RGB", (300, 150), color="white").save(screenshot)

    monkeypatch.setattr(executor, "capture_screen", lambda: Path(screenshot))

    captured = {}

    def fake_vlm_call(prompt, messages):
        captured["prompt"] = prompt
        captured["messages"] = messages
        return "DeepSeek API Docs"

    monkeypatch.setattr(executor, "get_vlm_call", lambda: ("fake", fake_vlm_call))

    result = executor.handle_browser_extract_text(
        ActionStep(
            action="browser_extract_text",
            params={
                "text": "DeepSeek API",
                "target": "first search result title",
                "visual_description": "Read the title of the first main search result.",
                "strategy_hint": "vlm_read",
            },
        )
    )

    assert result["status"] == "success"
    assert result["matched_text"] == "DeepSeek API Docs"
    assert "Read the title of the first main search result." in captured["prompt"]
    # Prompt can mention the search query, but visual_description should be preserved as the primary target.
