from backend.executor.actions_schema import ActionStep
import backend.executor.executor as executor


def test_handle_open_url_runs_verification(monkeypatch):
    monkeypatch.setattr(executor.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(
        executor,
        "_wait_for_ocr_targets",
        lambda targets, attempts=3, delay=0.8: {"success": True, "matched_text": "Example"},
    )

    result = executor.handle_open_url(
        ActionStep(
            action="open_url",
            params={"url": "example.com", "verify_text": ["Example Domain"]},
        )
    )

    assert result["status"] == "opened"
    assert result["verified"] is True
    assert result["verification"]["matched_text"] == "Example"
