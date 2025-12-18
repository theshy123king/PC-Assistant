import asyncio
import json
import threading
import time
from pathlib import Path

from backend.observability.store import EvidenceStore


def test_evidence_store_persistence_and_subscribe(tmp_path: Path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store = EvidenceStore(tmp_path, queue_maxsize=2)
    store.set_event_loop(loop)

    request_id = "req-test"

    def producer():
        for i in range(5):
            store.emit_sync(
                request_id,
                type="test",
                payload={"i": i},
                artifact_bytes=b"data" if i in (1, 3) else None,
                artifact_kind="json",
                artifact_mime="application/octet-stream",
                artifact_meta={"artifact_id": f"art-{i}"},
            )
            time.sleep(0.01)

    t = threading.Thread(target=producer)
    t.start()
    t.join()

    time.sleep(0.1)

    events_path = tmp_path / request_id / "events.jsonl"
    assert events_path.exists()
    lines = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 5
    parsed = [json.loads(ln) for ln in lines]
    seqs = [p["seq"] for p in parsed]
    assert seqs == sorted(seqs)

    for i in (1, 3):
        art_path = store.get_artifact_path(request_id, f"art-{i}")
        assert art_path.exists()
        assert art_path.read_bytes() == b"data"

    async def collect():
        collected = []
        async for ev in store.subscribe(request_id):
            collected.append(ev)
            if len(collected) >= 5:
                break
        return collected

    events = loop.run_until_complete(asyncio.wait_for(collect(), timeout=2))
    assert len(events) == 5
    assert [ev.seq for ev in events] == sorted(ev.seq for ev in events)
    assert any(ev.artifact for ev in events)

    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
