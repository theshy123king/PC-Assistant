from pathlib import Path
import tempfile

import mss


def capture_screen() -> Path:
    """Capture all monitors and save temporarily as screenshot.png."""
    output_path = Path(tempfile.gettempdir()) / "screenshot.png"
    with mss.mss() as sct:
        try:
            sct.shot(mon=-1, output=str(output_path))
        except Exception:
            # Fallback to primary monitor if all-monitors capture fails.
            sct.shot(output=str(output_path))
    return output_path
