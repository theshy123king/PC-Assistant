from pathlib import Path

from PIL import Image

from backend.executor.actions_schema import ActionStep
import backend.executor.executor as executor
from backend.vision.ocr import OcrBox


def test_handle_browser_click_runs_verification(monkeypatch, tmp_path):
    screenshot = tmp_path / "shot.png"
    Image.new("RGB", (200, 100), color="white").save(screenshot)

    monkeypatch.setattr(executor, "capture_screen", lambda: Path(screenshot))
    monkeypatch.setattr(
        executor,
        "run_ocr_with_boxes",
        lambda path: ("", [OcrBox(text="Login", x=10, y=20, width=40, height=20, conf=90.0)]),
    )
    monkeypatch.setattr(
        executor,
        "handle_click",
        lambda step: {"status": "clicked", "params": step.params},
    )
    verify_called = {}

    def fake_verify(targets, attempts=2, delay=0.8):
        verify_called["targets"] = targets
        return {"success": True, "matched_text": targets[0]}

    monkeypatch.setattr(executor, "_wait_for_ocr_targets", fake_verify)

    result = executor.handle_browser_click(
        ActionStep(
            action="browser_click",
            params={"text": "Login", "verify_text": ["Ready"]},
        )
    )

    assert result["status"] == "clicked"
    assert result.get("verified") is True
    assert verify_called["targets"] == ["Ready"]
