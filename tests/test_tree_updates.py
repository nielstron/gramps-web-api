"""Push transport, authentication, and publication ordering."""

import json
import time
from unittest.mock import Mock, patch

import fakeredis
import pytest
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from redis import ConnectionError

from gramps_webapi.api.resources.tree_updates import TreeUpdatesResource
from gramps_webapi.api.tree_updates import (
    publish_tree_update,
    stream_key,
    tree_event_stream,
    tree_updates_url,
)
from gramps_webapi.api.util import close_db


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config.update(
        JWT_SECRET_KEY="tree-update-test-key-which-is-long-enough",
        TREE_UPDATES_REDIS_URL="redis://localhost/0",
        TREE_UPDATES_ENABLED=True,
        CELERY_CONFIG={},
    )
    JWTManager(app)
    app.add_url_rule("/updates", view_func=TreeUpdatesResource.as_view("updates"))
    with patch("gramps_webapi.api.tree_updates.get_actor_name", return_value="Alex"):
        yield app


def payload(event):
    return json.loads(event.split("data: ", 1)[1])


def test_push_and_reconnect_without_database_polling(app):
    broker = fakeredis.FakeRedis(decode_responses=True)
    stream = tree_event_stream(broker, "tree1", "user1", time.time() + 60)
    assert payload(next(stream))["revision"] == "0-0"
    with (
        app.app_context(),
        patch("gramps_webapi.api.tree_updates.event_broker", return_value=broker),
    ):
        publish_tree_update("tree1", "user2")
    event = payload(next(stream))
    assert event["own"] is False
    assert event["actor_name"] == "Alex"
    assert event["changes"] == []
    assert broker.xlen(stream_key("tree2")) == 0
    stream.close()
    reconnect = tree_event_stream(broker, "tree1", "user1", time.time() + 60)
    assert payload(next(reconnect))["revision"] == event["revision"]
    reconnect.close()


def test_own_updates_and_bounded_history(app):
    broker = fakeredis.FakeRedis(decode_responses=True)
    stream = tree_event_stream(broker, "tree1", "user1", time.time() + 60)
    next(stream)
    with (
        app.app_context(),
        patch("gramps_webapi.api.tree_updates.event_broker", return_value=broker),
    ):
        for _ in range(105):
            publish_tree_update("tree1", "user1")
    assert broker.xlen(stream_key("tree1")) == 100
    assert payload(next(stream))["own"] is True
    stream.close()


def test_heartbeat_and_expiry():
    broker = Mock()
    broker.xrevrange.return_value = []
    broker.xread.return_value = []
    stream = tree_event_stream(broker, "tree1", "user1", 100)
    with patch("gramps_webapi.api.tree_updates.time.time", return_value=90):
        next(stream)
        assert next(stream) == ": keepalive\n\n"
        broker.xread.assert_called_once_with(
            {stream_key("tree1"): "0-0"}, count=100, block=10000
        )
    with patch("gramps_webapi.api.tree_updates.time.time", return_value=101):
        with pytest.raises(StopIteration):
            next(stream)


def test_stream_requires_auth_and_uses_token_tree(app):
    client = app.test_client()
    assert client.get("/updates").status_code == 401
    with app.app_context():
        token = create_access_token(
            identity="user1", additional_claims={"tree": "tree1"}
        )
    with patch("gramps_webapi.api.resources.tree_updates.tree_event_stream") as stream:
        stream.return_value = iter([": ready\n\n"])
        response = client.get(
            "/updates?tree=another-tree", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.mimetype == "text/event-stream"
        assert response.headers["X-Accel-Buffering"] == "no"
        assert stream.call_args.args[1:3] == ("tree1", "user1")
        response.close()


def test_publish_after_database_and_undo_log_close(app):
    db = Mock(readonly=False)
    db.undodb.change_summary.return_value = []
    db.get_save_path.return_value = "/trees/tree1"
    calls = []
    db.close.side_effect = lambda: calls.append("db closed")
    db.undodb.close.side_effect = lambda: calls.append("undo closed")
    with (
        app.app_context(),
        patch("gramps_webapi.api.tree_updates.publish_tree_update") as publish,
    ):
        publish.side_effect = lambda *args: calls.append("published")
        close_db(db)
        publish.assert_called_once_with("tree1", db.undodb.user_id, [])
    assert calls == ["db closed", "undo closed", "published"]


def test_reads_never_publish(app):
    with (
        app.app_context(),
        patch("gramps_webapi.api.tree_updates.publish_tree_update") as publish,
    ):
        close_db(Mock(readonly=True))
        publish.assert_not_called()


def test_broker_failure_does_not_fail_committed_save(app):
    with (
        app.app_context(),
        patch("gramps_webapi.api.tree_updates.event_broker") as broker,
    ):
        broker.return_value.xadd.side_effect = ConnectionError("unavailable")
        publish_tree_update("tree1", "user1")


def test_uses_existing_celery_redis_and_can_be_disabled(app):
    with app.app_context():
        app.config["TREE_UPDATES_REDIS_URL"] = None
        app.config["CELERY_CONFIG"] = {"broker_url": "redis://broker/1"}
        assert tree_updates_url() == "redis://broker/1"
        app.config["TREE_UPDATES_REDIS_URL"] = ""
        assert tree_updates_url() is None


def test_change_summary_permission_and_reconnect(app):
    broker = fakeredis.FakeRedis(decode_responses=True)
    changes = [{"type": "Source", "action": 1, "count": 1}]
    with (
        app.app_context(),
        patch("gramps_webapi.api.tree_updates.event_broker", return_value=broker),
    ):
        publish_tree_update("tree1", "user2", changes)
    for include_private in (False, True):
        stream = tree_event_stream(
            broker, "tree1", "user1", time.time() + 60, include_private=include_private
        )
        ready = payload(next(stream))
        assert ready["actor_name"] == "Alex"
        assert ready["changes"] == (changes if include_private else [])
        stream.close()
