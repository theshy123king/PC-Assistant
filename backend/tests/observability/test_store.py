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


def test_replay_skips_truncated_lines(tmp_path: Path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store = EvidenceStore(tmp_path)
    store.set_event_loop(loop)

    request_id = "req-replay"
    req_dir = tmp_path / request_id
    req_dir.mkdir(parents=True, exist_ok=True)
    events_path = req_dir / "events.jsonl"

    valid1 = {
        "request_id": request_id,
        "seq": 1,
        "ts_ms": int(time.time() * 1000),
        "type": "test",
        "payload": {"k": "v1"},
    }
    valid2 = {
        "request_id": request_id,
        "seq": 2,
        "ts_ms": int(time.time() * 1000),
        "type": "test",
        "payload": {"k": "v2"},
    }

    with events_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(valid1) + "\n")
        f.write('{"bad_json": ')  # truncated/invalid
        f.write("\n")
        f.write(json.dumps(valid2) + "\n")

    async def collect():
        out = []
        async for ev in store.subscribe(request_id, after_seq=0):
            out.append(ev)
            if len(out) >= 2:
                break
        return out

    events = loop.run_until_complete(asyncio.wait_for(collect(), timeout=2))
    assert len(events) == 2
    assert [e.seq for e in events] == [1, 2]

    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
