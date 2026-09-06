#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2026 Gramps Web contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#

"""Regression tests for consecutive and concurrent media metadata writes."""

import datetime
import os
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Any, ClassVar
from unittest.mock import patch

from gramps.cli.clidbman import CLIDbManager
from gramps.gen.dbstate import DbState
from PIL import Image

from gramps_webapi import dbloader
from gramps_webapi.api.resources import base as base_resource
from gramps_webapi.app import create_app
from gramps_webapi.auth import add_user, user_db
from gramps_webapi.auth.const import ROLE_OWNER
from gramps_webapi.const import ENV_CONFIG_FILE, TEST_AUTH_CONFIG


class TestMediaMetadataStability(unittest.TestCase):
    """Exercise media upload followed by metadata updates through an API key."""

    app: ClassVar[Any]
    headers: ClassVar[dict[str, str]]

    @classmethod
    def setUpClass(cls):
        cls.name = "Test media metadata stability"
        cls.dbman = CLIDbManager(DbState())
        dbpath, _ = cls.dbman.create_new_db_cli(cls.name, dbid="sqlite")
        tree = os.path.basename(dbpath)

        # CLIDbManager registered the database plugins already. Avoid scanning
        # optional GUI plugins again when opening the same tree in the API.
        dbloader._plugins_registered = True

        cls.media_base_dir = tempfile.mkdtemp()
        cls.user_db_path = os.path.join(cls.media_base_dir, "users.sqlite")
        with patch.dict("os.environ", {ENV_CONFIG_FILE: TEST_AUTH_CONFIG}):
            cls.app = create_app(
                config={
                    "TESTING": True,
                    "RATELIMIT_ENABLED": False,
                    "MEDIA_BASE_DIR": cls.media_base_dir,
                    "USER_DB_URI": f"sqlite:///{cls.user_db_path}",
                },
                config_from_env=False,
            )
        with cls.app.app_context():
            user_db.create_all()
            add_user(name="owner", password="123", role=ROLE_OWNER, tree=tree)

        client = cls.app.test_client()
        response = client.post(
            "/api/token/", json={"username": "owner", "password": "123"}
        )
        owner_headers = {"Authorization": f"Bearer {response.json['access_token']}"}
        expires_on = datetime.date.today() + datetime.timedelta(days=2)
        response = client.post(
            "/api/users/-/api-keys/",
            json={"name": "Media test", "expires_on": expires_on.isoformat()},
            headers=owner_headers,
        )
        cls.headers = {"Authorization": f"Bearer {response.json['token']}"}

    @classmethod
    def tearDownClass(cls):
        cls.dbman.remove_database(cls.name)
        shutil.rmtree(cls.media_base_dir)

    @staticmethod
    def _image(color: int) -> bytes:
        output = BytesIO()
        Image.new("RGB", (8, 8), (color, 1, 2)).save(output, "PNG")
        return output.getvalue()

    def _upload(self, color: int) -> tuple[str, dict]:
        client = self.app.test_client()
        response = client.post(
            "/api/media/",
            data=self._image(color),
            headers=self.headers,
            content_type="image/png",
        )
        self.assertEqual(response.status_code, 201)
        handle = response.json[0]["new"]["handle"]
        media = client.get(f"/api/media/{handle}", headers=self.headers).json
        media["gramps_id"] = f"M{color}"
        media["desc"] = f"Evidence image {color}"
        return handle, media

    def test_concurrent_metadata_updates_after_upload(self):
        """Concurrent uploaded-media edits must wait instead of failing locked."""
        media = [self._upload(color) for color in (101, 102)]
        barrier = threading.Barrier(len(media))
        parse_object = base_resource.GrampsObjectResourceHelper._parse_object

        def synchronized_parse(resource):
            obj = parse_object(resource)
            barrier.wait(timeout=10)
            return obj

        def update(item):
            handle, obj = item
            client = self.app.test_client()
            response = client.put(
                f"/api/media/{handle}", json=obj, headers=self.headers
            )
            return response.status_code

        with (
            patch.object(base_resource, "run_task", return_value=None),
            patch.object(
                base_resource.GrampsObjectResourceHelper,
                "_parse_object",
                synchronized_parse,
            ),
        ):
            with ThreadPoolExecutor(max_workers=len(media)) as pool:
                statuses = list(pool.map(update, media))

        self.assertEqual(statuses, [200] * len(media))

        client = self.app.test_client()
        for handle, expected in media:
            response = client.get(f"/api/media/{handle}", headers=self.headers)
            self.assertEqual(response.json["gramps_id"], expected["gramps_id"])
            self.assertEqual(response.json["desc"], expected["desc"])


if __name__ == "__main__":
    unittest.main()
