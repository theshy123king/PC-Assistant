from pathlib import Path

from PIL import Image

from backend.executor.actions_schema import ActionStep
import backend.executor.executor as executor
from backend.vision.ocr import OcrBox


def test_handle_browser_input_types_into_field(monkeypatch, tmp_path):
    screenshot = tmp_path / "shot.png"
    Image.new("RGB", (200, 100), color="white").save(screenshot)

    monkeypatch.setattr(executor, "capture_screen", lambda: Path(screenshot))
    monkeypatch.setattr(
        executor,
        "run_ocr_with_boxes",
        lambda path: ("", [OcrBox(text="Search", x=10, y=20, width=50, height=20, conf=90.0)]),
    )
    monkeypatch.setattr(
        executor,
        "handle_click",
        lambda step: {"status": "clicked", "params": step.params},
    )

    typed = {}

    def fake_type(params):
        typed["value"] = params.get("text")
        return "typed"

    monkeypatch.setattr(executor.input, "type_text", fake_type)

    verify_called = {}

    def fake_verify(targets, attempts=1, delay=0.0):
        verify_called["called"] = targets
        return {"success": True, "matched_text": targets[0]}

    monkeypatch.setattr(executor, "_wait_for_ocr_targets", fake_verify)

    result = executor.handle_browser_input(
        ActionStep(
            action="browser_input",
            params={
                "text": "Search",
                "value": "hello world",
                "variants": ["搜索"],
                "verify_text": ["done"],
            },
        )
    )

    assert result["status"] == "typed"
    assert result["matched_text"] == "Search"
    assert typed["value"] == "hello world"
    assert result.get("verified") is True
    assert verify_called["called"] == ["done"]
