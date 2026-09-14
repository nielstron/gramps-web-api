#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2024      David Straub
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""Tests for the database repair endpoint."""

import os
import unittest
from io import BytesIO
from PIL import Image
from unittest.mock import patch

from gramps.cli.clidbman import CLIDbManager
from gramps.gen.dbstate import DbState

from gramps_webapi.app import create_app
from gramps_webapi.auth import add_user, user_db
from gramps_webapi.auth.const import ROLE_GUEST, ROLE_OWNER
from gramps_webapi.const import ENV_CONFIG_FILE, TEST_AUTH_CONFIG


class TestRepair(unittest.TestCase):
    """Test database repair."""

    @classmethod
    def setUpClass(cls):
        cls.name = "Test Web API Repair"
        cls.dbman = CLIDbManager(DbState())
        dirpath, _ = cls.dbman.create_new_db_cli(cls.name, dbid="sqlite")
        tree = os.path.basename(dirpath)
        with patch.dict("os.environ", {ENV_CONFIG_FILE: TEST_AUTH_CONFIG}):
            cls.app = create_app(config_from_env=False)
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        with cls.app.app_context():
            user_db.create_all()
            add_user(name="user", password="123", role=ROLE_GUEST, tree=tree)
            add_user(name="owner", password="123", role=ROLE_OWNER, tree=tree)
        rv = cls.client.post(
            "/api/token/", json={"username": "owner", "password": "123"}
        )
        access_token = rv.json["access_token"]
        cls.headers = {"Authorization": f"Bearer {access_token}"}

    @classmethod
    def tearDownClass(cls):
        cls.dbman.remove_database(cls.name)

    def test_repair_empty_database(self):
        """Test Repairing the empty database."""
        rv = self.client.post("/api/trees/-/repair", headers=self.headers)
        assert rv.status_code == 201
        assert rv.json["num_errors"] == 0
        assert rv.json["message"] == ""

    def test_thumbnail_repair_and_upload_pregeneration(self):
        """Uploaded images and the repair job populate the actual endpoint cache."""
        data = BytesIO()
        Image.new("RGB", (50, 80), "red").save(data, format="PNG")
        with patch("gramps_webapi.api.thumbnails.THUMBNAIL_SIZES", (40,)):
            rv = self.client.post(
                "/api/media/",
                data=data.getvalue(),
                content_type="image/png",
                headers=self.headers,
            )
            assert rv.status_code == 201
            handle = rv.json[0]["new"]["handle"]
            path = f"/api/media/{handle}/thumbnail/40?square=false&thumbnail_version=2"
            with patch(
                "gramps_webapi.api.file.LocalFileHandler.send_thumbnail",
                side_effect=AssertionError("cache miss"),
            ):
                cached = self.client.get(path, headers=self.headers)
                assert cached.status_code == 200
                assert Image.open(BytesIO(cached.data)).size == (25, 40)
            repaired = self.client.post(
                "/api/trees/-/repair/thumbnails", headers=self.headers
            )
            assert repaired.status_code == 201
            assert repaired.json == {"processed": 1, "generated": 2, "errors": []}
            # Replacing the file must generate thumbnails under its new checksum.
            data = BytesIO()
            Image.new("RGB", (80, 50), "blue").save(data, format="PNG")
            rv = self.client.put(
                f"/api/media/{handle}/file",
                data=data.getvalue(),
                content_type="image/png",
                headers=self.headers,
            )
            assert rv.status_code == 200
            with patch(
                "gramps_webapi.api.file.LocalFileHandler.send_thumbnail",
                side_effect=AssertionError("cache miss"),
            ):
                cached = self.client.get(path, headers=self.headers)
                assert Image.open(BytesIO(cached.data)).size == (40, 25)
        self.client.delete(f"/api/media/{handle}", headers=self.headers)

    def test_thumbnail_repair_permissions(self):
        rv = self.client.post(
            "/api/token/", json={"username": "user", "password": "123"}
        )
        headers = {"Authorization": "Bearer " + rv.json["access_token"]}
        assert (
            self.client.post(
                "/api/trees/-/repair/thumbnails", headers=headers
            ).status_code
            == 403
        )
        assert self.client.post("/api/trees/-/repair/thumbnails").status_code == 401

    def test_small_media_encodes_native_size_once_per_shape(self):
        from gramps_webapi.api.image import save_image_buffer

        data = BytesIO()
        Image.new("RGB", (50, 80), "red").save(data, format="PNG")
        with (
            patch("gramps_webapi.api.thumbnails.THUMBNAIL_SIZES", (100, 200, 600)),
            patch(
                "gramps_webapi.api.thumbnails.save_image_buffer",
                wraps=save_image_buffer,
            ) as encode,
        ):
            rv = self.client.post(
                "/api/media/",
                data=data.getvalue(),
                content_type="image/png",
                headers=self.headers,
            )
            assert rv.status_code == 201
            assert encode.call_count == 2
            assert [call.args[0].size for call in encode.call_args_list] == [
                (50, 80),
                (50, 50),
            ]
            handle = rv.json[0]["new"]["handle"]
            for size in (100, 200, 600):
                result = self.client.get(
                    f"/api/media/{handle}/thumbnail/{size}", headers=self.headers
                )
                assert Image.open(BytesIO(result.data)).size == (50, 80)
        self.client.delete(f"/api/media/{handle}", headers=self.headers)

    def test_repair_empty_person(self):
        """Test Repairing an empty person."""
        rv = self.client.post("/api/people/", json={}, headers=self.headers)
        assert rv.status_code == 201
        rv = self.client.get("/api/people/", headers=self.headers)
        assert rv.status_code == 200
        assert len(rv.json) == 1
        rv = self.client.post("/api/trees/-/repair", headers=self.headers)
        assert rv.status_code == 201
        assert rv.json["num_errors"] == 1
        rv = self.client.get("/api/people/", headers=self.headers)
        assert rv.status_code == 200
        assert len(rv.json) == 0

    def test_repair_empty_event(self):
        """Test Repairing an empty event."""
        rv = self.client.post("/api/events/", json={}, headers=self.headers)
        assert rv.status_code == 201
        rv = self.client.get("/api/events/", headers=self.headers)
        assert rv.status_code == 200
        assert len(rv.json) == 1
        rv = self.client.post("/api/trees/-/repair", headers=self.headers)
        assert rv.status_code == 201
        assert rv.json["num_errors"] == 1
        rv = self.client.get("/api/events/", headers=self.headers)
        assert rv.status_code == 200
        assert len(rv.json) == 0
