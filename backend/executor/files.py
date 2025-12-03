"""
Safe file operations used by the action executor.

All functions return structured status dictionaries instead of raising to keep
callers resilient and machine-readable.
"""

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List


def _is_path_safe(path: str) -> bool:
    r"""
    Reject operations in sensitive system locations.

    Disallows:
    - C:\
    - C:\Windows
    - C:\Program Files
    - C:\Program Files (x86)
    - C:\Users\<username>\AppData
    """
    if not path:
        return False

    abs_path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath("C:\\"))

    username = os.environ.get("USERNAME", "").strip()
    forbidden = {
        root,
        os.path.join(root, "windows"),
        os.path.join(root, "program files"),
        os.path.join(root, "program files (x86)"),
    }
    if username:
        forbidden.add(os.path.join(root, "users", username, "appdata"))

    for prefix in forbidden:
        if abs_path == prefix or abs_path.startswith(prefix + os.sep):
            return False

    return True


def _resolve_path(path: str, base_dir: str | None) -> str:
    """
    Resolve a potentially relative path against an optional base directory.
    Falls back to the current working directory when the base is missing.
    """
    if not path:
        return path
    try:
        candidate = Path(path)
        if not candidate.is_absolute():
            if base_dir:
                base = Path(base_dir).expanduser()
                if base.exists() and base.is_dir():
                    candidate = base / candidate
                else:
                    candidate = Path.cwd() / candidate
            else:
                candidate = Path.cwd() / candidate
        return str(candidate.resolve())
    except Exception:
        return os.path.abspath(path)


def _abs(path: str) -> str:
    return os.path.abspath(path)


def _error(action: str, reason: str) -> Dict[str, Any]:
    return {"status": "error", "action": action, "reason": reason}


def _success(action: str, **kwargs: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"status": "success", "action": action}
    result.update(kwargs)
    return result


def list_files(params: Dict[str, Any]) -> Dict[str, Any]:
    """List the contents of a directory with basic metadata."""
    action = "list_files"
    params = params or {}
    base_dir = params.get("base_dir")
    path = _resolve_path(params.get("path"), base_dir)
    if not path or not isinstance(path, str):
        return _error(action, "'path' is required")
    if not _is_path_safe(path):
        return _error(action, "path is not allowed")
    if not os.path.exists(path):
        return _error(action, f"path does not exist '{path}'")
    if not os.path.isdir(path):
        return _error(action, f"path is not a directory '{path}'")

    try:
        entries: List[Dict[str, Any]] = []
        for name in sorted(os.listdir(path)):
            full = _abs(os.path.join(path, name))
            entry: Dict[str, Any] = {
                "name": name,
                "path": full,
                "is_dir": os.path.isdir(full),
            }
            if os.path.isfile(full):
                try:
                    entry["size"] = os.path.getsize(full)
                except Exception:
                    entry["size"] = None
            entries.append(entry)
        return _success(action, path=_abs(path), entries=entries, count=len(entries))
    except Exception as exc:  # noqa: BLE001
        return _error(action, f"failed to list files: {exc}")


def delete_file(params: Dict[str, Any]) -> Dict[str, Any]:
    """Delete a file after safety checks."""
    action = "delete_file"
    params = params or {}
    base_dir = params.get("base_dir")
    path = _resolve_path(params.get("path"), base_dir)
    if not path or not isinstance(path, str):
        return _error(action, "'path' is required")
    if not os.path.exists(path):
        return _error(action, f"path does not exist '{path}'")
    if os.path.isdir(path):
        return _error(action, "deleting directories is not supported")
    if not _is_path_safe(path):
        return _error(action, "path is not allowed")

    try:
        os.remove(path)
        return _success(action, path=_abs(path), deleted=True)
    except Exception as exc:  # noqa: BLE001
        return _error(action, f"failed to delete file: {exc}")


def rename_file(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Rename a file within its directory after safety checks.

    Expected params:
        source: str - existing file path.
        new_name: str - new file name (no path separators).
    """
    action = "rename_file"
    params = params or {}
    base_dir = params.get("base_dir")
    source = _resolve_path(params.get("source"), base_dir)
    new_name = params.get("new_name")

    if not source or not isinstance(source, str):
        return _error(action, "'source' is required")
    if not new_name or not isinstance(new_name, str):
        return _error(action, "'new_name' is required")
    if os.path.basename(new_name) != new_name:
        return _error(action, "'new_name' must not contain path separators")
    if not os.path.exists(source):
        return _error(action, f"source does not exist '{source}'")
    if os.path.isdir(source):
        return _error(action, "renaming directories is not supported")
    if not _is_path_safe(source):
        return _error(action, "source path is not allowed")

    target = os.path.join(os.path.dirname(source), new_name)
    if not _is_path_safe(target):
        return _error(action, "target path is not allowed")
    if os.path.exists(target):
        return _error(action, f"target already exists '{target}'")

    try:
        os.rename(source, target)
        return _success(action, source=_abs(source), destination=_abs(target))
    except Exception as exc:  # noqa: BLE001
        return _error(action, f"failed to rename file: {exc}")


def move_file(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Move a file into a destination directory after safety checks.

    Expected params:
        source: str - existing file path.
        destination_dir: str - existing directory path.
    """
    action = "move_file"
    params = params or {}
    base_dir = params.get("base_dir")
    source = _resolve_path(params.get("source"), base_dir)
    dest_dir = _resolve_path((params.get("destination_dir") or params.get("destination")), base_dir)

    if not source or not isinstance(source, str):
        return _error(action, "'source' is required")
    if not dest_dir or not isinstance(dest_dir, str):
        return _error(action, "'destination_dir' is required")
    if not os.path.exists(source):
        return _error(action, f"source does not exist '{source}'")
    if os.path.isdir(source):
        return _error(action, "moving directories is not supported")
    if not os.path.isdir(dest_dir):
        return _error(action, f"destination_dir is not a directory '{dest_dir}'")
    if not _is_path_safe(source):
        return _error(action, "source path is not allowed")
    if not _is_path_safe(dest_dir):
        return _error(action, "destination path is not allowed")

    target = os.path.join(dest_dir, os.path.basename(source))
    if os.path.exists(target):
        return _error(action, f"target already exists '{target}'")
    if not _is_path_safe(target):
        return _error(action, "target path is not allowed")

    try:
        shutil.move(source, target)
        return _success(
            action,
            source=_abs(source),
            destination=_abs(target),
            moved=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(action, f"failed to move file: {exc}")


def copy_file(params: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a file into a destination directory after safety checks."""
    action = "copy_file"
    params = params or {}
    base_dir = params.get("base_dir")
    source = _resolve_path(params.get("source"), base_dir)
    dest_dir = _resolve_path((params.get("destination_dir") or params.get("destination")), base_dir)

    if not source or not isinstance(source, str):
        return _error(action, "'source' is required")
    if not dest_dir or not isinstance(dest_dir, str):
        return _error(action, "'destination_dir' is required")
    if not os.path.exists(source):
        return _error(action, f"source does not exist '{source}'")
    if os.path.isdir(source):
        return _error(action, "copying directories is not supported")
    if not os.path.isdir(dest_dir):
        return _error(action, f"destination_dir is not a directory '{dest_dir}'")
    if not _is_path_safe(source):
        return _error(action, "source path is not allowed")
    if not _is_path_safe(dest_dir):
        return _error(action, "destination path is not allowed")

    target = os.path.join(dest_dir, os.path.basename(source))
    if os.path.exists(target):
        return _error(action, f"target already exists '{target}'")
    if not _is_path_safe(target):
        return _error(action, "target path is not allowed")

    try:
        shutil.copy2(source, target)
        return _success(
            action,
            source=_abs(source),
            destination=_abs(target),
            copied=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(action, f"failed to copy file: {exc}")


def write_file(params: Dict[str, Any]) -> Dict[str, Any]:
    """Create or overwrite a file with given content after safety checks."""
    action = "write_file"
    params = params or {}
    base_dir = params.get("base_dir")
    path = _resolve_path(params.get("path"), base_dir)
    content = params.get("content")

    if not path or not isinstance(path, str):
        return _error(action, "'path' is required")
    if content is None or not isinstance(content, str):
        return _error(action, "'content' is required and must be string")
    if not _is_path_safe(path):
        return _error(action, "path is not allowed")

    try:
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            return _error(action, f"parent directory does not exist '{parent}'")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return _success(action, path=_abs(path), bytes_written=len(content.encode("utf-8")))
    except Exception as exc:  # noqa: BLE001
        return _error(action, f"failed to write file: {exc}")
