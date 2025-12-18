import tempfile
from contextlib import contextmanager
from pathlib import Path

import backend.executor.executor as executor
from backend.executor.actions_schema import ActionPlan, ActionStep
from backend.executor.task_context import TaskContext


@contextmanager
def _sandbox_dir():
    # Keep test files under the repository to satisfy path safety checks.
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
        yield Path(tmp)


def test_list_files_returns_entries_and_count():
    with _sandbox_dir() as base:
        (base / "a.txt").write_text("hello")
        (base / "subdir").mkdir()

        result = executor.handle_list_files(ActionStep(action="list_files", params={"path": str(base)}))

        assert result["status"] == "success"
        names = {entry["name"] for entry in result["entries"]}
        assert result["count"] == len(result["entries"])
        assert "a.txt" in names
        assert "subdir" in names


def test_move_file_supports_destination_alias_and_moves_file():
    with _sandbox_dir() as base:
        src = base / "note.txt"
        dest_dir = base / "dest"
        dest_dir.mkdir()
        src.write_text("content")

        result = executor.handle_move_file(
            ActionStep(action="move_file", params={"source": str(src), "destination": str(dest_dir)})
        )

        expected_target = dest_dir / "note.txt"
        assert result["status"] == "success"
        assert result["destination"] == str(expected_target.resolve())
        assert expected_target.exists()
        assert not src.exists()


def test_copy_file_creates_duplicate_in_destination_dir():
    with _sandbox_dir() as base:
        src = base / "data.bin"
        dest_dir = base / "copies"
        dest_dir.mkdir()
        src.write_text("payload")

        result = executor.handle_copy_file(
            ActionStep(action="copy_file", params={"source": str(src), "destination_dir": str(dest_dir)})
        )

        expected_target = dest_dir / "data.bin"
        assert result["status"] == "success"
        assert result["destination"] == str(expected_target.resolve())
        assert expected_target.exists()
        assert src.exists()


def test_delete_file_removes_file():
    with _sandbox_dir() as base:
        victim = base / "remove.me"
        victim.write_text("bye")

        result = executor.handle_delete_file(ActionStep(action="delete_file", params={"path": str(victim)}))

        assert result["status"] == "success"
        assert result["deleted"] is True
        assert not victim.exists()


def test_write_file_creates_or_overwrites():
    with _sandbox_dir() as base:
        target = base / "newfile.txt"
        params = {"path": str(target), "content": "hello world"}

        result = executor.handle_write_file(ActionStep(action="write_file", params=params))

        assert result["status"] == "success"
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "hello world"


def test_rewrite_save_pattern_to_write_file():
    with _sandbox_dir() as base:
        target = base / "save_me.txt"
        plan = ActionPlan(
            task="ui save",
            steps=[
                ActionStep(action="type_text", params={"text": "hello", "auto_enter": False}),
                ActionStep(action="key_press", params={"keys": ["ctrl", "s"]}),
                ActionStep(action="type_text", params={"text": str(target)}),
            ],
        )
        ctx = TaskContext(user_instruction="save via ui")
        result = executor.run_steps(
            plan,
            context=ctx,
            allow_replan=False,
            capture_observations=False,
            max_retries=0,
            capture_ocr=False,
            consent_token=True,
        )
        assert result["overall_status"] in {"success", "replanned"}
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "hello"
