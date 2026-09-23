"""Tree change notifications shared by web processes and background workers."""

import json
import logging
import time
from functools import lru_cache

from flask import current_app
from redis import Redis, RedisError

_LOG = logging.getLogger(__name__)
HEARTBEAT_SECONDS = 25


def tree_updates_url() -> str | None:
    """Use the configured event broker, or the existing Redis Celery broker."""
    if not current_app.config["TREE_UPDATES_ENABLED"]:
        return None
    url = current_app.config["TREE_UPDATES_REDIS_URL"]
    if url is None:
        url = current_app.config["CELERY_CONFIG"].get("broker_url", "")
    return url if url.startswith(("redis://", "rediss://", "unix://")) else None


@lru_cache(maxsize=4)
def event_broker(url: str) -> Redis:
    """Reuse a connection pool, with a separate connection for each blocked read."""
    return Redis.from_url(
        url, decode_responses=True, socket_connect_timeout=3, socket_timeout=35
    )


def stream_key(tree: str) -> str:
    return f"gramps:tree-updates:{tree}"


def publish_tree_update(tree: str, user_id: str | None) -> None:
    """Publish after the writable database closes and its caches invalidate.

    A broker outage must not turn an already committed save into an API error.
    No object names, handles, or other private genealogy data enter the stream.
    """
    url = tree_updates_url()
    if not url:
        return
    try:
        event_broker(url).xadd(
            stream_key(tree), {"actor": user_id or ""}, maxlen=100, approximate=False
        )
    except RedisError:
        _LOG.warning("Could not publish tree update", exc_info=True)


def encode_event(revision: str, own: bool, event: str = "changed") -> str:
    data = json.dumps({"revision": revision, "own": own})
    return f"event: {event}\ndata: {data}\n\n"


def tree_event_stream(broker, tree: str, user_id: str, expires: float):
    """Block on Redis events; heartbeat comments never query the tree database.

    The initial snapshot lets a reconnecting client detect changes it missed,
    even if the bounded event history has already been trimmed.
    """
    key = stream_key(tree)
    latest = broker.xrevrange(key, count=1)
    cursor = latest[0][0] if latest else "0-0"
    yield encode_event(cursor, False, "ready")
    while time.time() < expires:
        timeout = max(1, int(min(HEARTBEAT_SECONDS, expires - time.time()) * 1000))
        messages = broker.xread({key: cursor}, count=100, block=timeout)
        if time.time() >= expires:
            return
        if not messages:
            yield ": keepalive\n\n"
            continue
        entries = messages[0][1]
        cursor = entries[-1][0]
        own = all(entry[1]["actor"] == user_id for entry in entries)
        yield encode_event(cursor, own)
