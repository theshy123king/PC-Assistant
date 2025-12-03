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
