#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2022      David Straub
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

"""Tests for the `gramps_webapi.api.resources.user` module."""

import os
import re
import unittest
from unittest.mock import MagicMock, patch

from gramps.cli.clidbman import CLIDbManager
from gramps.gen.dbstate import DbState

from gramps_webapi.app import create_app
from gramps_webapi.auth import add_user, user_db
from gramps_webapi.auth.const import (
    ROLE_ADMIN,
    ROLE_MEMBER,
)
from gramps_webapi.const import ENV_CONFIG_FILE, TEST_AUTH_CONFIG

from . import BASE_URL


class TestConfig(unittest.TestCase):
    """Test cases for the /api/config/ endpoints."""

    def setUp(self):
        self.name = "Test Web API"
        self.dbman = CLIDbManager(DbState())
        dirpath, _name = self.dbman.create_new_db_cli(self.name, dbid="sqlite")
        tree = os.path.basename(dirpath)
        with patch.dict("os.environ", {ENV_CONFIG_FILE: TEST_AUTH_CONFIG}):
            self.app = create_app(
                config={"TESTING": True, "RATELIMIT_ENABLED": False},
                config_from_env=False,
            )
        self.client = self.app.test_client()
        with self.app.app_context():
            user_db.create_all()
            add_user(
                name="user",
                password="123",
                email="test1@example.com",
                role=ROLE_MEMBER,
                tree=tree,
            )
            add_user(
                name="admin",
                password="123",
                email="test2@example.com",
                role=ROLE_ADMIN,
                tree=tree,
            )
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        self.header_member = {"Authorization": f"Bearer {rv.json['access_token']}"}
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "admin", "password": "123"}
        )
        self.header_owner = {"Authorization": f"Bearer {rv.json['access_token']}"}

    def tearDown(self):
        self.ctx.pop()
        self.dbman.remove_database(self.name)

    def test_get_config(self):
        rv = self.client.get(
            f"{BASE_URL}/config/",
            headers=self.header_member,
        )
        assert rv.status_code == 403
        rv = self.client.get(
            f"{BASE_URL}/config/",
            headers=self.header_owner,
        )
        assert rv.status_code == 200
        assert rv.json == {}

    def test_set_config_unauth(self):
        rv = self.client.put(
            f"{BASE_URL}/config/EMAIL_HOST/",
            headers=self.header_member,
            json={"value": "myhost"},
        )
        assert rv.status_code == 403

    def test_set_config_put(self):
        rv = self.client.put(
            f"{BASE_URL}/config/EMAIL_HOST/",
            headers=self.header_owner,
            json={"value": "host1"},
        )
        assert rv.status_code == 200
        rv = self.client.get(
            f"{BASE_URL}/config/EMAIL_HOST/", headers=self.header_owner
        )
        assert rv.status_code == 200
        assert rv.json == "host1"

    def test_config_delete(self):
        rv = self.client.put(
            f"{BASE_URL}/config/EMAIL_HOST/",
            headers=self.header_owner,
            json={"value": "host2"},
        )
        rv = self.client.get(
            f"{BASE_URL}/config/EMAIL_HOST/", headers=self.header_owner
        )
        assert rv.status_code == 200
        assert rv.json == "host2"
        rv = self.client.delete(
            f"{BASE_URL}/config/EMAIL_HOST/",
            headers=self.header_owner,
        )
        rv = self.client.get(
            f"{BASE_URL}/config/EMAIL_HOST/", headers=self.header_owner
        )
        assert rv.status_code == 404

    def test_config_reset_password(self):
        """Check that the config options are picked up in the reset email."""

        def get_from_host():
            with patch("gramps_webapi.api.util.smtplib.SMTP_SSL") as mock_smtp:
                mock_smtp_instance = MagicMock()
                mock_smtp.return_value = mock_smtp_instance
                self.client.post(f"{BASE_URL}/users/user/password/reset/trigger/")
                mock_smtp_instance.send_message.assert_called_once()
                msg = mock_smtp_instance.send_message.call_args[0][0]
                body = msg.get_body().get_payload().replace("=\n", "")
                matches = re.findall(r".*(https?://[^/]+)/api", body)
                host = matches[0]
                return msg["From"], host

        from_email, host = get_from_host()
        assert from_email == ""
        assert host == "http://localhost"
        self.client.put(
            f"{BASE_URL}/config/BASE_URL/",
            headers=self.header_owner,
            json={"value": "https://www.example.com"},
        )
        self.client.put(
            f"{BASE_URL}/config/DEFAULT_FROM_EMAIL/",
            headers=self.header_owner,
            json={"value": "from@example.com"},
        )
        from_email, host = get_from_host()
        assert from_email == "from@example.com"
        assert host == "https://www.example.com"

    def test_email_config(self):
        rv = self.client.get(
            f"{BASE_URL}/config/email/",
            headers=self.header_member,
        )
        assert rv.status_code == 403

        self.client.put(
            f"{BASE_URL}/config/EMAIL_HOST_PASSWORD/",
            headers=self.header_owner,
            json={"value": "secret"},
        )
        rv = self.client.get(
            f"{BASE_URL}/config/email/",
            headers=self.header_owner,
        )
        assert rv.status_code == 200
        assert rv.json == {
            "host": "localhost",
            "port": 465,
            "username": "",
            "from_email": "",
            "from_name": "",
            "security": "ssl",
            "password_set": True,
        }
        assert "password" not in rv.json

    @patch("gramps_webapi.api.resources.config.send_email")
    def test_update_email_config_and_send_test(self, mock_send_email):
        rv = self.client.put(
            f"{BASE_URL}/config/email/",
            headers=self.header_owner,
            json={
                "host": "smtp.example.com",
                "port": 587,
                "username": "mailer",
                "password": "secret",
                "from_email": "family@example.com",
                "from_name": "Bond family",
                "security": "starttls",
            },
        )
        assert rv.status_code == 200

        rv = self.client.get(
            f"{BASE_URL}/config/email/",
            headers=self.header_owner,
        )
        assert rv.status_code == 200
        assert rv.json == {
            "host": "smtp.example.com",
            "port": 587,
            "username": "mailer",
            "from_email": "family@example.com",
            "from_name": "Bond family",
            "security": "starttls",
            "password_set": True,
        }

        rv = self.client.post(
            f"{BASE_URL}/config/email/test/",
            headers=self.header_owner,
            json={"recipient": "admin@example.com"},
        )
        assert rv.status_code == 200
        mock_send_email.assert_called_once_with(
            subject="Gramps Web test email",
            body="This is a test email sent from the Gramps Web administration settings.",
            to=["admin@example.com"],
        )

    def test_ai_settings_permissions_validation_and_secret_redaction(self):
        payload = {
            "enabled": True,
            "chat_model": "qwen3:8b",
            "chat_base_url": "http://ollama:11434/v1",
            "chat_api_key": "chat-secret",
            "embedding_model": "embedding-model",
            "embedding_base_url": "http://ollama:11434",
            "embedding_api_key": "embedding-secret",
        }
        endpoint = f"{BASE_URL}/config/ai/"
        assert self.client.get(endpoint, headers=self.header_member).status_code == 403
        assert (
            self.client.put(
                endpoint, headers=self.header_member, json=payload
            ).status_code
            == 403
        )
        for patch_data in [
            {"chat_model": ""},
            {"chat_base_url": "file:///etc/passwd"},
            {"chat_base_url": "https://secret@example.com"},
        ]:
            assert (
                self.client.put(
                    endpoint, headers=self.header_owner, json={**payload, **patch_data}
                ).status_code
                == 422
            )
        response = self.client.put(endpoint, headers=self.header_owner, json=payload)
        assert response.status_code == 200
        assert response.json["chat_api_key_set"] is True
        assert response.json["embedding_api_key_set"] is True
        assert "secret" not in response.text
        assert (
            "secret"
            not in self.client.get(
                f"{BASE_URL}/config/", headers=self.header_owner
            ).text
        )
        assert (
            self.client.get(
                f"{BASE_URL}/config/AI_SETTINGS/", headers=self.header_owner
            ).status_code
            == 404
        )
        assert (
            self.client.put(
                f"{BASE_URL}/config/AI_SETTINGS/",
                headers=self.header_owner,
                json={"value": "{}"},
            ).status_code
            == 404
        )
        from gramps_webapi.api.util import get_config

        assert get_config("LLM_MODEL") == "qwen3:8b"
        assert get_config("LLM_API_KEY") == "chat-secret"
        payload.pop("chat_api_key")
        payload["embedding_api_key"] = ""
        payload["enabled"] = False
        response = self.client.put(endpoint, headers=self.header_owner, json=payload)
        assert response.status_code == 200
        assert response.json["chat_api_key_set"] is True
        assert response.json["embedding_api_key_set"] is False
        assert get_config("LLM_MODEL") == ""
        assert get_config("VECTOR_EMBEDDING_MODEL") == ""
        # Settings retain the model while disabled and are shared by a new app context.
        with self.app.app_context():
            from gramps_webapi.ai_config import get_ai_config

            assert get_ai_config()["LLM_MODEL"] == "qwen3:8b"

    def test_embedding_settings_refresh_cached_function_without_restart(self):
        from gramps_webapi.api.search.embeddings import get_embedding_function

        endpoint = f"{BASE_URL}/config/ai/"
        payload = {
            "enabled": True,
            "chat_model": "model",
            "chat_base_url": "http://provider/v1",
            "embedding_model": "vectors",
            "embedding_base_url": "http://provider/v1",
            "embedding_api_key": "first",
        }
        assert (
            self.client.put(
                endpoint, headers=self.header_owner, json=payload
            ).status_code
            == 200
        )
        first, model = get_embedding_function()
        assert model == "vectors"
        assert get_embedding_function()[0] is first
        payload["embedding_api_key"] = "second"
        assert (
            self.client.put(
                endpoint, headers=self.header_owner, json=payload
            ).status_code
            == 200
        )
        second, _model = get_embedding_function()
        assert second is not first
        with patch("gramps_webapi.api.search.embeddings.requests.post") as post:
            post.return_value.json.return_value = {
                "data": [{"index": 0, "embedding": [0.1, 0.2]}]
            }
            assert second(["test"])[0] == [0.1, 0.2]
            assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer second"
        payload["enabled"] = False
        assert (
            self.client.put(
                endpoint, headers=self.header_owner, json=payload
            ).status_code
            == 200
        )
        with self.assertRaises(ValueError):
            get_embedding_function()
