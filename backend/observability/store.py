from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional, Set

from backend.contracts.evidence import ArtifactRef, EvidenceEvent

logger = logging.getLogger(__name__)


class EvidenceStore:
    """
    Thread-safe evidence store with async subscribers.

    - Producers emit_sync(...) enqueue lightweight events without blocking.
    - A background writer thread handles all disk I/O and publishes to async queues via call_soon_threadsafe.
    - Subscribers can replay persisted events then stream live events for a request_id.
    """

    def __init__(self, root_dir: Path, queue_maxsize: int = 200) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.ingress_q: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=queue_maxsize)
        self._subs: Dict[str, Set[asyncio.Queue]] = {}
        self._seq: Dict[str, int] = {}
        self._dropped_count: int = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._writer_thread = threading.Thread(target=self._writer_loop, name="evidence-writer", daemon=True)
        self._writer_thread.start()

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Configure the asyncio loop used to publish to subscribers."""
        self._loop = loop

    def emit_sync(
        self,
        request_id: str,
        type: str,
        payload: Dict[str, Any],
        step_index: Optional[int] = None,
        attempt: Optional[int] = None,
        artifact_bytes: Optional[bytes] = None,
        artifact_kind: Optional[str] = None,
        artifact_mime: Optional[str] = None,
        artifact_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Non-blocking event emission from synchronous producers (e.g., executor thread).

        On queue overflow, drops silently and increments a counter (never blocks).
        """
        item = {
            "request_id": request_id,
            "type": type,
            "payload": payload or {},
            "step_index": step_index,
            "attempt": attempt,
            "artifact_bytes": artifact_bytes,
            "artifact_kind": artifact_kind,
            "artifact_mime": artifact_mime,
            "artifact_meta": artifact_meta or {},
        }
        try:
            self.ingress_q.put_nowait(item)
        except queue.Full:
            self._dropped_count += 1
            logger.warning("EvidenceStore queue full; dropped_count=%s", self._dropped_count)

    def get_artifact_path(self, request_id: str, artifact_id: str) -> Path:
        return self.root_dir / request_id / "artifacts" / artifact_id

    async def subscribe(self, request_id: str, after_seq: int = 0) -> AsyncIterator[EvidenceEvent]:
        """
        Replay persisted events then stream live events for the given request.
        """
        req_dir = self.root_dir / request_id
        events_path = req_dir / "events.jsonl"

        if events_path.exists():
            try:
                with events_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except Exception:
                            continue
                        try:
                            ev = EvidenceEvent(**data)
                        except Exception:
                            continue
                        if ev.seq > after_seq:
                            yield ev
            except FileNotFoundError:
                pass

        live_q: asyncio.Queue = asyncio.Queue(maxsize=200)
        subs = self._subs.setdefault(request_id, set())
        subs.add(live_q)
        try:
            while True:
                ev: EvidenceEvent = await live_q.get()
                if ev.seq <= after_seq:
                    continue
                yield ev
        finally:
            subs.discard(live_q)

    def _writer_loop(self) -> None:
        while True:
            item = self.ingress_q.get()
            try:
                self._process_item(item)
            except Exception as exc:  # noqa: BLE001
                logger.exception("EvidenceStore writer failed: %s", exc)

    def _process_item(self, item: Dict[str, Any]) -> None:
        request_id = item.get("request_id") or "unknown"
        now_ms = int(time.time() * 1000)
        seq = self._seq.get(request_id, 0) + 1
        self._seq[request_id] = seq

        artifact_ref = None
        artifact_bytes: Optional[bytes] = item.get("artifact_bytes")
        artifact_kind: Optional[str] = item.get("artifact_kind")
        artifact_mime: Optional[str] = item.get("artifact_mime")
        artifact_meta: Dict[str, Any] = item.get("artifact_meta") or {}

        if artifact_bytes:
            artifact_id = artifact_meta.get("artifact_id") or uuid.uuid4().hex
            artifact_path = self.get_artifact_path(request_id, artifact_id)
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(artifact_bytes)
            artifact_ref = ArtifactRef(
                artifact_id=artifact_id,
                kind=(artifact_kind or "image"),
                mime=artifact_mime or "application/octet-stream",
                bytes=len(artifact_bytes),
                width=artifact_meta.get("width"),
                height=artifact_meta.get("height"),
                sha256=artifact_meta.get("sha256"),
            )

        event = EvidenceEvent(
            request_id=request_id,
            seq=seq,
            ts_ms=now_ms,
            type=item.get("type") or "unknown",
            payload=item.get("payload") or {},
            step_index=item.get("step_index"),
            attempt=item.get("attempt"),
            artifact=artifact_ref,
        )

        req_dir = self.root_dir / request_id
        req_dir.mkdir(parents=True, exist_ok=True)
        events_path = req_dir / "events.jsonl"
        with events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.model_dump(), ensure_ascii=False) + "\n")

        self._publish_live(request_id, event)

    def _publish_live(self, request_id: str, event: EvidenceEvent) -> None:
        if not self._loop or self._loop.is_closed():
            return
        queues = list(self._subs.get(request_id, set()))
        for q in queues:
            def _push(queue_ref: asyncio.Queue = q, ev: EvidenceEvent = event) -> None:
                try:
                    queue_ref.put_nowait(ev)
                except asyncio.QueueFull:
                    pass

            try:
                self._loop.call_soon_threadsafe(_push)
            except RuntimeError:
                continue


__all__ = ["EvidenceStore"]
