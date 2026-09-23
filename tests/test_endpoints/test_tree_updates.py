"""Verify real API mutations reach the push stream after cache invalidation."""

import time
import unittest
from unittest.mock import patch

import fakeredis

from gramps_webapi.api.tree_updates import tree_event_stream, stream_key

from . import get_test_client
from .util import fetch_header


class TestTreeUpdates(unittest.TestCase):
    def test_insert_edit_delete_publish(self):
        client = get_test_client()
        broker = fakeredis.FakeRedis(decode_responses=True)
        with (
            patch.dict(
                client.application.config,
                {
                    "VECTOR_EMBEDDING_MODEL": "",
                    "LLM_MODEL": "",
                    "TREE_UPDATES_REDIS_URL": "redis://localhost/0",
                    "TREE_UPDATES_ENABLED": True,
                },
            ),
            patch("gramps_webapi.api.tree_updates.event_broker", return_value=broker),
        ):
            headers = fetch_header(client, empty_db=True)
            # Establish the subscriber using the same tree as the test user's JWT.
            from flask_jwt_extended import decode_token

            with client.application.app_context():
                tree = decode_token(headers["Authorization"].split()[1])["tree"]
            stream = tree_event_stream(broker, tree, "another-user", time.time() + 60)
            next(stream)
            created = client.post(
                "/api/notes/", json={"text": {"string": "Push test"}}, headers=headers
            )
            self.assertEqual(created.status_code, 201, created.json)
            self.assertGreater(broker.xlen(stream_key(tree)), 0)
            self.assertIn('"own": false', next(stream))
            handle = created.json[0]["handle"]
            url = f"/api/notes/{handle}"
            note = client.get(url, headers=headers).json
            note["text"]["string"] = "Edited push test"
            self.assertEqual(
                client.put(url, json=note, headers=headers).status_code, 200
            )
            self.assertIn('"own": false', next(stream))
            self.assertEqual(client.delete(url, headers=headers).status_code, 200)
            self.assertIn('"own": false', next(stream))
            stream.close()
