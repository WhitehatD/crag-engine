# coding: utf-8
"""Phase 10 — In-memory subscriber registry + broadcast queue.

The daemon holds a set of asyncio.Queue instances, one per connected SSE client.
When something noteworthy happens (insight saved, principle distilled, etc.),
the producer calls `broadcast(kind, payload_dict)` which fans out to every queue
non-blockingly (slow consumers are dropped — see SUBSCRIBER_QUEUE_MAX).

Design:
  - In-memory: subscribers come and go fast; persisting them is wasteful.
  - bounded queue per client: if a client falls behind by SUBSCRIBER_QUEUE_MAX
    events, oldest events are dropped (better than blocking the producer).
  - audit log: every broadcast is written to broadcast_events (queryable via /query/broadcasts).
"""
import asyncio
import json
import logging
import time

logger = logging.getLogger("crag-anchor")

SUBSCRIBER_QUEUE_MAX = 64
HEARTBEAT_INTERVAL_SEC = 25  # SSE keepalive

_subscribers: set[asyncio.Queue] = set()
_lock = asyncio.Lock()


async def add_subscriber() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
    async with _lock:
        _subscribers.add(queue)
    logger.info("Phase 10: SSE subscriber added — total=%d", len(_subscribers))
    return queue


async def remove_subscriber(queue: asyncio.Queue) -> None:
    async with _lock:
        _subscribers.discard(queue)
    logger.info("Phase 10: SSE subscriber removed — total=%d", len(_subscribers))


async def broadcast(kind: str, payload: dict, persist_fn=None) -> int:
    """Push to all subscribers. Returns count of subscribers reached.
    persist_fn(kind, payload_json, count) is called once for audit logging.
    """
    msg = {"kind": kind, "ts": time.time(), "payload": payload}
    msg_json = json.dumps(msg, default=str)
    reached = 0
    async with _lock:
        # snapshot so we don't hold the lock during queue puts
        snapshot = list(_subscribers)
    for q in snapshot:
        try:
            q.put_nowait(msg_json)
            reached += 1
        except asyncio.QueueFull:
            # slow consumer — drop the oldest, push the new
            try:
                _ = q.get_nowait()
                q.put_nowait(msg_json)
                reached += 1
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Phase 10: subscriber put failed: %s", exc)
    if persist_fn:
        try:
            persist_fn(kind, msg_json, reached)
        except Exception as exc:
            logger.warning("Phase 10: broadcast persist failed: %s", exc)
    return reached
