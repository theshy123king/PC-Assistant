"""Entrypoint for launching the PC-Assistant backend with port hygiene.

This script:
- Checks whether the configured HTTP port is free.
- If the port is held by an existing PC-Assistant/uvicorn process, it will
  terminate that process to clear stale listeners.
- If the port is held by another process, it aborts with a clear message
  instead of raising a permission error.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
import urllib.request
from typing import Iterable

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    psutil = None

import uvicorn

from backend.config import DEV_HOST, DEV_PORT, is_test_mode, resolve_host_port

APP_PATH = "backend.app:app"


def log(message: str) -> None:
    print(f"[backend-launch] {message}", flush=True)


def _can_bind(host: str, port: int) -> bool:
    """Return True if the socket can be bound, False otherwise."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _iter_listeners_on_port(port: int) -> Iterable["psutil.Process"]:
    """Yield processes listening on the given TCP port."""
    if psutil is None:
        return []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            for conn in proc.connections(kind="inet"):
                if conn.status != psutil.CONN_LISTEN:
                    continue
                if conn.laddr.port != port:
                    continue
                yield proc
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue


def _is_assistant_process(proc: "psutil.Process") -> bool:
    """Best-effort heuristic to decide if the process is ours."""
    if psutil is None:
        return False
    try:
        name = (proc.info.get("name") or proc.name() or "").lower()
        cmdline_list = proc.info.get("cmdline") or proc.cmdline() or []
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False

    cmdline = " ".join(cmdline_list).lower()
    return "uvicorn" in cmdline and ("backend.app:app" in cmdline or "pc_assistant" in cmdline)


def _terminate_process(proc: "psutil.Process", timeout: float = 3.0) -> bool:
    """Attempt graceful termination, then force kill if needed."""
    if psutil is None:
        return False
    try:
        log(f"Terminating process pid={proc.pid} name={proc.name()}")
        proc.terminate()
        proc.wait(timeout=timeout)
        log(f"Process pid={proc.pid} terminated cleanly")
        return True
    except (psutil.TimeoutExpired, psutil.NoSuchProcess):
        pass

    try:
        log(f"Forcing kill on pid={proc.pid}")
        proc.kill()
        proc.wait(timeout=timeout)
        log(f"Process pid={proc.pid} killed")
        return True
    except (psutil.TimeoutExpired, psutil.NoSuchProcess):
        log(f"Failed to kill pid={proc.pid}; port may remain busy")
        return False


def ensure_port_free(host: str, port: int) -> str:
    """
    Make sure the port is available, cleaning up stale assistant listeners.

    Returns:
    - "free": safe to launch
    - "running": backend already healthy on the port
    - "blocked": port in use by something else
    - "unknown": unexpected failure
    """
    if psutil is None:
        log("psutil not installed; skipping port cleanup and proceeding to launch.")
        return "free"

    if _can_bind(host, port):
        return "free"

    log(f"Port {port} on {host} is busy; scanning for listeners...")
    # If something is already serving our backend, treat as success.
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=1) as resp:
            if resp.status == 200:
                log(f"Backend already running on http://{host}:{port}; not starting a new one.")
                return "running"
    except Exception:
        pass
    for proc in _iter_listeners_on_port(port):
        if not _is_assistant_process(proc):
            log(
                f"Port {port} is in use by pid={proc.pid} "
                f"({proc.name()}); not terminating unrelated process."
            )
            return "blocked"

        if _terminate_process(proc):
            # Give the OS a moment to release the socket.
            for _ in range(10):
                time.sleep(0.3)
                if _can_bind(host, port):
                    log(f"Port {port} cleared after terminating pid={proc.pid}")
                    return True
            log(f"Port {port} on {host} still busy after terminating pid={proc.pid}")
            return "blocked"
        return "blocked"

    log(f"Port {port} on {host} is in use, but listener details are unavailable. Please free it manually.")
    return "unknown"


def main(host: str | None = None, port: int | None = None, reload: bool = False) -> None:
    resolved_host, resolved_port = resolve_host_port(host=host, port=port)
    profile = "test" if is_test_mode() else "dev"

    availability = ensure_port_free(resolved_host, resolved_port)
    if availability == "running":
        sys.exit(0)
    if availability in ("blocked", "unknown"):
        sys.exit(1)

    log(f"Starting uvicorn {APP_PATH} on http://{resolved_host}:{resolved_port} [{profile}]")
    uvicorn.run(
        APP_PATH,
        host=resolved_host,
        port=resolved_port,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Launch the PC-Assistant backend with port checks.")
    parser.add_argument("--host", help=f"Host to bind (default dev: {DEV_HOST})")
    parser.add_argument(
        "--port",
        type=int,
        help=f"Port to bind (default dev: {DEV_PORT}, test: see PC_ASSISTANT_TEST_PORT)",
    )
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn reload (dev only)")
    args = parser.parse_args()

    main(host=args.host, port=args.port, reload=args.reload)
