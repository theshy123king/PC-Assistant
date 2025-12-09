"""Shared configuration helpers for host/port selection."""

from __future__ import annotations

import os
from typing import Tuple

# Dedicated ports for different runtimes to avoid clashes and lingering sockets.
DEV_HOST = os.getenv("PC_ASSISTANT_DEV_HOST", "127.0.0.1")
DEV_PORT = int(os.getenv("PC_ASSISTANT_DEV_PORT", "5004"))
TEST_HOST = os.getenv("PC_ASSISTANT_TEST_HOST", DEV_HOST)
TEST_PORT = int(os.getenv("PC_ASSISTANT_TEST_PORT", "5015"))


def is_test_mode() -> bool:
    """Detect pytest/EXECUTOR_TEST_MODE runs."""
    return os.getenv("EXECUTOR_TEST_MODE") == "1" or bool(os.getenv("PYTEST_CURRENT_TEST"))


def resolve_host_port(host: str | None = None, port: int | None = None) -> Tuple[str, int]:
    """Return the host/port tuple for the current mode, honoring overrides."""
    if host and port:
        return host, int(port)

    if is_test_mode():
        resolved_host = host or TEST_HOST
        resolved_port = int(port or TEST_PORT)
    else:
        resolved_host = host or DEV_HOST
        resolved_port = int(port or DEV_PORT)

    return resolved_host, resolved_port
