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


def get_actor_name(user_id: str | None) -> str:
    """Resolve the editor once on publication, never on heartbeat."""
    from ..auth import User, user_db

    user = user_db.session.get(User, user_id) if user_id else None
    return (user.fullname or user.name) if user else ""


def publish_tree_update(
    tree: str, user_id: str | None, changes: list | None = None
) -> None:
    """Publish after the writable database closes and its caches invalidate.

    A broker outage must not turn an already committed save into an API error.
    Object names and handles never enter the stream; counts are permission gated.
    """
    url = tree_updates_url()
    if not url:
        return
    actor_name = get_actor_name(user_id)
    try:
        event_broker(url).xadd(
            stream_key(tree),
            {
                "actor": user_id or "",
                "actor_name": actor_name,
                "changes": json.dumps(changes or []),
            },
            maxlen=100,
            approximate=False,
        )
    except RedisError:
        _LOG.warning("Could not publish tree update", exc_info=True)


def encode_event(
    revision: str, own: bool, event: str = "changed", details: dict | None = None
) -> str:
    data = json.dumps({"revision": revision, "own": own, **(details or {})})
    return f"event: {event}\ndata: {data}\n\n"


def tree_event_stream(
    broker, tree: str, user_id: str, expires: float, *, include_private: bool = False
):
    """Block on Redis events; heartbeat comments never query the tree database.

    The initial snapshot lets a reconnecting client detect changes it missed,
    even if the bounded event history has already been trimmed.
    """
    key = stream_key(tree)
    latest = broker.xrevrange(key, count=1)
    cursor = latest[0][0] if latest else "0-0"

    def details(entry):
        return {
            "actor_name": entry.get("actor_name", ""),
            "changes": (
                json.loads(entry.get("changes", "[]")) if include_private else []
            ),
        }

    yield encode_event(
        cursor, False, "ready", details(latest[0][1]) if latest else None
    )
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
        # Preserve individual editor/summary pairs while the client coalesces refreshes.
        for revision, entry in entries:
            yield encode_event(
                revision, entry["actor"] == user_id, details=details(entry)
            )
