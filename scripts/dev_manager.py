"""PC-Assistant development manager.

One-command helpers:
- start-backend: run the FastAPI/uvicorn backend (uses launcher with port hygiene).
- start-frontend: run the Electron app.
- stop: terminate assistant-related processes (backend launcher/uvicorn/electron).
- ports: show current port usage for dev/test ports.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, List

import psutil

try:
    from backend.config import DEV_HOST, DEV_PORT, TEST_HOST, TEST_PORT
except Exception:  # pragma: no cover
    DEV_HOST, DEV_PORT = "127.0.0.1", 5004
    TEST_HOST, TEST_PORT = DEV_HOST, 5015


ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
LOG_DIR = ROOT / "logs"
DEV_MANAGER_LOG = LOG_DIR / "dev_manager.log"
MAX_LOG_BYTES = 1_000_000


def _log(msg: str) -> None:
    print(f"[dev-manager] {msg}", flush=True)


def _ensure_log_dir() -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            try:
                backup.unlink()
            except Exception:
                pass
            path.rename(backup)
    except Exception:
        pass


def start_backend(reload: bool = False) -> int:
    cmd: List[str] = [sys.executable, "-m", "backend.launch_backend"]
    if reload:
        cmd.append("--reload")
    _log(f"Starting backend: {' '.join(cmd)}")
    _ensure_log_dir()
    _rotate_if_needed(DEV_MANAGER_LOG)
    with DEV_MANAGER_LOG.open("a", encoding="utf-8") as f:
        return subprocess.call(cmd, cwd=ROOT, stdout=f, stderr=f)


def start_frontend() -> int:
    cmd = ["npm", "start"]
    _log(f"Starting frontend (Electron): {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=FRONTEND_DIR, shell=os.name == "nt")


def _matches_assistant(proc: psutil.Process) -> bool:
    try:
        name = (proc.info.get("name") or proc.name() or "").lower()
        cmd = " ".join(proc.info.get("cmdline") or proc.cmdline() or []).lower()
        cwd = (proc.cwd() or "").lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False

    root_str = str(ROOT).lower()
    if root_str in cwd:
        return True
    if "backend.launch_backend" in cmd or "backend.app:app" in cmd:
        return True
    if "uvicorn" in cmd and "backend" in cmd:
        return True
    if "electron" in name and "frontend" in cmd:
        return True
    if "node" in name and "electron" in cmd and "frontend" in cmd:
        return True
    return False


def iter_assistant_processes() -> Iterable[psutil.Process]:
    for proc in psutil.process_iter(["pid", "name", "cmdline", "cwd"]):
        if _matches_assistant(proc):
            yield proc


def stop_processes(timeout: float = 3.0) -> None:
    procs = list(iter_assistant_processes())
    if not procs:
        _log("No assistant-related processes found.")
        return

    _log(f"Stopping {len(procs)} process(es)...")
    for proc in procs:
        try:
            _log(f"Terminating pid={proc.pid} name={proc.name()}")
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    deadline = time.time() + timeout
    remaining = [p for p in procs if p.is_running()]
    while remaining and time.time() < deadline:
        time.sleep(0.2)
        remaining = [p for p in remaining if p.is_running()]

    for proc in remaining:
        try:
            _log(f"Killing pid={proc.pid} name={proc.name()}")
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    _log("Stop command finished.")


def _connections_for_ports(ports: Iterable[int]) -> List[psutil._common.sconn]:  # type: ignore[attr-defined]
    results = []
    target_ports = set(ports)
    try:
        conns = psutil.net_connections(kind="inet")
    except Exception:
        return results
    for conn in conns:
        if conn.laddr and conn.laddr.port in target_ports:
            results.append(conn)
    return results


def show_ports() -> None:
    ports = [DEV_PORT, TEST_PORT]
    conns = _connections_for_ports(ports)
    if not conns:
        _log("No listeners found on dev/test ports.")
        _log(f"Dev port {DEV_PORT} ({DEV_HOST}), Test port {TEST_PORT} ({TEST_HOST})")
        return

    _log("Current port usage:")
    for conn in conns:
        pid = conn.pid or "?"
        try:
            proc = psutil.Process(conn.pid) if conn.pid else None
            name = proc.name() if proc else "?"
            cmd = " ".join(proc.cmdline()) if proc else "?"
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            name, cmd = "?", "?"
        _log(
            f"  port {conn.laddr.port} pid={pid} status={conn.status} "
            f"proc={name} cmd={cmd}"
        )


def tail_logs(path: Path, lines: int = 100) -> None:
    try:
        with path.open("r", encoding="utf-8") as f:
            content = f.readlines()
        for line in content[-lines:]:
            print(line.rstrip())
    except FileNotFoundError:
        _log(f"Log file not found: {path}")
    except Exception as exc:
        _log(f"Failed to read log {path}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="PC-Assistant development manager.")
    sub = parser.add_subparsers(dest="command", required=True)

    start_backend_cmd = sub.add_parser("start-backend", help="Start backend via launcher")
    start_backend_cmd.add_argument("--reload", action="store_true", help="Enable uvicorn reload")

    sub.add_parser("start-frontend", help="Start Electron frontend (npm start)")
    sub.add_parser("stop", help="Stop assistant-related processes")
    sub.add_parser("ports", help="Show dev/test port usage")
    tail_cmd = sub.add_parser("tail", help="Tail a log file")
    tail_cmd.add_argument("log", nargs="?", default="dev_manager", help="Which log (dev_manager|backend|main|renderer)")
    tail_cmd.add_argument("-n", "--lines", type=int, default=100, help="Number of lines")

    args = parser.parse_args()

    if args.command == "start-backend":
        sys.exit(start_backend(reload=args.reload))
    if args.command == "start-frontend":
        sys.exit(start_frontend())
    if args.command == "stop":
        stop_processes()
        sys.exit(0)
    if args.command == "ports":
        show_ports()
        sys.exit(0)
    if args.command == "tail":
        mapping = {
            "dev_manager": DEV_MANAGER_LOG,
            "backend": ROOT / "logs" / "backend.log",
            "main": ROOT / "logs" / "electron-main.log",
            "renderer": ROOT / "logs" / "electron-renderer.log",
        }
        target = mapping.get(args.log, Path(args.log))
        tail_logs(target, lines=args.lines)
        sys.exit(0)


if __name__ == "__main__":
    main()
