#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2020-2023      David Straub
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
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from celery.result import AsyncResult
from gramps.cli.clidbman import CLIDbManager
from gramps.gen.dbstate import DbState

from gramps_webapi.api.tasks import send_email_invitation
from gramps_webapi.app import create_app
from gramps_webapi.auth import (
    User,
    UserInvitation,
    add_user,
    create_oidc_account,
    delete_user,
    get_all_user_details,
    get_guid,
    get_number_users,
    get_user_details,
    get_user_oidc_accounts,
    modify_user,
    set_user_settings,
    user_db,
)
from gramps_webapi.auth.const import (
    ROLE_ADMIN,
    ROLE_DISABLED,
    ROLE_MEMBER,
    ROLE_OWNER,
    ROLE_UNCONFIRMED,
)
from gramps_webapi.const import ENV_CONFIG_FILE, TEST_AUTH_CONFIG
from gramps_webapi.dbmanager import WebDbManager

from . import BASE_URL
from .util import fetch_header


class TestUser(unittest.TestCase):
    """Test cases for the /api/user endpoints."""

    def setUp(self):
        self.name = "Test Web API"
        self.dbman = CLIDbManager(DbState())
        dbpath, _ = self.dbman.create_new_db_cli(self.name, dbid="sqlite")
        self.tree = os.path.basename(dbpath)
        dbpath2, _ = self.dbman.create_new_db_cli("Test Web API 2", dbid="sqlite")
        self.tree = os.path.basename(dbpath)
        self.tree2 = os.path.basename(dbpath2)
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
                email="test@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )
            add_user(
                name="user2",
                password="123",
                email="test2@example.com",
                role=ROLE_MEMBER,
                tree=self.tree2,
            )
            add_user(
                name="owner",
                password="123",
                email="owner@example.com",
                role=ROLE_OWNER,
                tree=self.tree,
            )
            add_user(
                name="owner2",
                password="123",
                email="owner2@example.com",
                role=ROLE_OWNER,
                tree=self.tree2,
            )
            add_user(
                name="admin",
                password="123",
                email="admin@example.com",
                role=ROLE_ADMIN,
                tree=self.tree,
            )
        self.assertTrue(self.app.testing)
        self.ctx = self.app.test_request_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.dbman.remove_database(self.name)

    def test_change_password_wrong_method(self):
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token = rv.json["access_token"]
        rv = self.client.get(
            BASE_URL + "/users/-/password/change",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert rv.status_code == 405

    def test_reset_password_form_preserves_application_prefix(self):
        with patch("gramps_webapi.api.resources.user.run_task") as task:
            self.client.post(BASE_URL + "/users/user/password/reset/trigger/")
        token = task.call_args.kwargs["token"]
        response = self.client.get(
            BASE_URL + "/users/-/password/reset/",
            query_string={"jwt": token},
        )
        assert response.status_code == 200
        assert "window.location.pathname" in response.text
        assert "fetch(`/api/" not in response.text

    def test_invite_with_registration_disabled(self):
        self.app.config["REGISTRATION_DISABLED"] = True
        login = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        header = {"Authorization": f"Bearer {login.json['access_token']}"}
        with patch("gramps_webapi.api.resources.invitations.run_task") as task:
            response = self.client.post(
                BASE_URL + "/users/-/invitations/",
                headers=header,
                json={"email": "invited@example.com", "role": ROLE_MEMBER},
            )
        assert response.status_code == 201, response.text
        assert (
            self.client.post(
                BASE_URL + "/users/uninvited/register/",
                json={
                    "email": "public@example.com",
                    "full_name": "Public",
                    "password": "123",
                    "tree": self.tree,
                },
            ).status_code
            == 405
        )
        token = task.call_args.kwargs["token"]
        invitation_header = {"Authorization": f"Bearer {token}"}
        response = self.client.post(
            BASE_URL + "/users/-/invite/",
            headers=invitation_header,
            json={
                "name": "invited",
                "full_name": "Invited Person",
                "password": "chosen password",
            },
        )
        assert response.status_code == 201, response.text
        details = get_user_details("invited")
        assert details["email"] == "invited@example.com"
        assert details["full_name"] == "Invited Person"
        assert details["role"] == ROLE_MEMBER
        assert details["tree"] == self.tree
        assert (
            self.client.post(
                BASE_URL + "/token/",
                json={"username": "invited", "password": "chosen password"},
            ).status_code
            == 200
        )
        assert (
            self.client.post(
                BASE_URL + "/users/-/invite/",
                headers=invitation_header,
                json={"name": "replay", "full_name": "Replay", "password": "password"},
            ).status_code
            == 409
        )

    def _login_header(self, name="owner"):
        response = self.client.post(
            BASE_URL + "/token/", json={"username": name, "password": "123"}
        )
        return {"Authorization": f"Bearer {response.json['access_token']}"}

    def test_match_home_person_sets_only_own_account_settings(self):
        from tests.test_home_person import make_person

        modify_user("user", fullname="Nils Muendler")
        endpoint = BASE_URL + "/users/-/settings/home-person/match"
        header = self._login_header("user")
        assert self.client.post(endpoint).status_code == 401
        with patch("gramps_webapi.api.resources.user.get_db_handle") as db:
            db.return_value.iter_people.return_value = [
                make_person("I1", "Niels", "Mündler")
            ]
            response = self.client.post(endpoint, headers=header)
        assert response.status_code == 200
        assert response.json == {"homePerson": "I1"}
        assert (
            self.client.get(BASE_URL + "/users/-/settings", headers=header).json
            == response.json
        )
        assert (
            self.client.get(
                BASE_URL + "/users/-/settings", headers=self._login_header()
            ).json
            == {}
        )

    def test_match_home_person_preserves_explicit_choices_and_other_settings(self):
        modify_user("user", fullname="Niels Mündler")
        user_id = get_guid("user")
        header = self._login_header("user")
        endpoint = BASE_URL + "/users/-/settings/home-person/match"
        for choice in ("I42", "", None):
            settings = {"homePerson": choice, "appearance": {"theme": "dark"}}
            set_user_settings(user_id, settings)
            with patch("gramps_webapi.api.resources.user.get_db_handle") as db:
                assert self.client.post(endpoint, headers=header).json == settings
                db.assert_not_called()
        set_user_settings(user_id, {"appearance": {"theme": "dark"}})
        with (
            patch("gramps_webapi.api.resources.user.get_db_handle"),
            patch(
                "gramps_webapi.api.resources.user.find_home_person", return_value="I1"
            ),
        ):
            assert self.client.post(endpoint, headers=header).json == {
                "homePerson": "I1",
                "appearance": {"theme": "dark"},
            }

    def test_match_home_person_does_not_overwrite_concurrent_settings(self):
        modify_user("user", fullname="Niels Mündler")
        user_id = get_guid("user")

        def concurrent_choice(*args, **kwargs):
            set_user_settings(user_id, {"homePerson": "I42"})
            return "I1"

        with (
            patch("gramps_webapi.api.resources.user.get_db_handle"),
            patch(
                "gramps_webapi.api.resources.user.find_home_person",
                side_effect=concurrent_choice,
            ),
        ):
            response = self.client.post(
                BASE_URL + "/users/-/settings/home-person/match",
                headers=self._login_header("user"),
            )
        assert response.json == {"homePerson": "I42"}

    def test_match_home_person_without_name_does_not_scan_tree(self):
        with patch("gramps_webapi.api.resources.user.get_db_handle") as db:
            response = self.client.post(
                BASE_URL + "/users/-/settings/home-person/match",
                headers=self._login_header("user"),
            )
        assert response.json == {}
        db.assert_not_called()

    def _invite(self, email="invite@example.com", role=ROLE_MEMBER):
        with patch("gramps_webapi.api.resources.invitations.run_task") as task:
            response = self.client.post(
                BASE_URL + "/users/-/invitations/",
                headers=self._login_header(),
                json={"email": email, "role": role},
            )
        assert response.status_code == 201, response.text
        token = task.call_args.kwargs["token"]
        return response.json, {"Authorization": f"Bearer {token}"}

    def test_invitation_permissions_and_tree_boundaries(self):
        endpoint = BASE_URL + "/users/-/invitations/"
        payload = {"email": "invite@example.com", "role": ROLE_MEMBER}
        assert self.client.post(endpoint, json=payload).status_code == 401
        member = self._login_header("user")
        assert (
            self.client.post(endpoint, headers=member, json=payload).status_code == 403
        )
        owner = self._login_header()
        assert (
            self.client.post(
                endpoint, headers=owner, json={**payload, "role": ROLE_ADMIN}
            ).status_code
            == 403
        )
        assert (
            self.client.post(
                endpoint, headers=owner, json={**payload, "tree": self.tree2}
            ).status_code
            == 403
        )
        for invalid in (
            {"email": "invalid", "role": 1},
            {**payload, "role": -1},
            {**payload, "role": 6},
        ):
            assert (
                self.client.post(endpoint, headers=owner, json=invalid).status_code
                == 422
            )
        invitation, token_header = self._invite()
        other = self._login_header("owner2")
        assert self.client.get(endpoint, headers=other).json == []
        assert self.client.get(endpoint, headers=member).status_code == 403
        assert (
            self.client.get(BASE_URL + "/users/", headers=token_header).status_code
            == 401
        )
        detail_endpoint = endpoint + invitation["id"] + "/"
        assert self.client.post(detail_endpoint, headers=other).status_code == 403
        assert self.client.delete(detail_endpoint, headers=other).status_code == 403
        assert (
            self.client.post(
                BASE_URL + "/users/-/password/reset/",
                headers=token_header,
                json={"new_password": "123"},
            ).status_code
            == 403
        )
        listed = self.client.get(endpoint, headers=owner).json
        assert listed == [invitation]
        assert "secret_hash" not in listed[0]

    def test_invitation_resend_replaces_token_and_revoke_disables_it(self):
        invitation, old_header = self._invite()
        endpoint = BASE_URL + "/users/-/invitations/" + invitation["id"] + "/"
        accept = BASE_URL + "/users/-/invite/"
        with patch("gramps_webapi.api.resources.invitations.run_task") as task:
            assert (
                self.client.post(endpoint, headers=self._login_header()).status_code
                == 200
            )
        header = {"Authorization": f"Bearer {task.call_args.kwargs['token']}"}
        assert self.client.get(accept, headers=old_header).status_code == 409
        assert self.client.get(accept, headers=header).status_code == 200
        assert (
            self.client.delete(endpoint, headers=self._login_header()).status_code
            == 200
        )
        assert self.client.get(accept, headers=header).status_code == 409

    def test_invitation_expiry_and_duplicate_email(self):
        invitation, header = self._invite("INVITE@example.com")
        owner = self._login_header()
        for email in ("invite@example.com", "TEST@example.com"):
            assert (
                self.client.post(
                    BASE_URL + "/users/-/invitations/",
                    headers=owner,
                    json={"email": email, "role": ROLE_MEMBER},
                ).status_code
                == 409
            )
        pending = user_db.session.get(UserInvitation, invitation["id"])
        pending.expires_at = datetime.now(timezone.utc).replace(
            tzinfo=None
        ) - timedelta(seconds=1)
        user_db.session.commit()
        assert (
            self.client.get(BASE_URL + "/users/-/invite/", headers=header).status_code
            == 410
        )

    def test_invitation_rejects_role_tampering_and_preserves_link_after_name_collision(
        self,
    ):
        _, header = self._invite()
        endpoint = BASE_URL + "/users/-/invite/"
        payload = {"name": "user", "full_name": "New Name", "password": "new password"}
        assert (
            self.client.post(
                endpoint, headers=header, json={**payload, "role": ROLE_ADMIN}
            ).status_code
            == 422
        )
        assert (
            self.client.post(endpoint, headers=header, json=payload).status_code == 409
        )
        for name in ("-", "_", " ", "bad/name"):
            assert (
                self.client.post(
                    endpoint, headers=header, json={**payload, "name": name}
                ).status_code
                == 422
            )
        assert (
            self.client.post(
                endpoint, headers=header, json={**payload, "name": "available"}
            ).status_code
            == 201
        )
        assert get_user_details("available")["role"] == ROLE_MEMBER

    def test_invitation_email_contains_prefixed_setup_link(self):
        self.app.config["BASE_URL"] = "https://example.com/stammbaum/"
        with patch("gramps_webapi.api.util.smtplib.SMTP_SSL") as smtp:
            send_email_invitation(email="invite@example.com", token="test-token")
        message = smtp.return_value.send_message.call_args.args[0]
        assert message["To"] == "invite@example.com"
        plain = message.get_body(preferencelist=("plain",)).get_content()
        assert (
            "https://example.com/stammbaum/api/users/-/invite/?jwt=test-token" in plain
        )
        assert "7 days" in plain

    def test_user_settings_are_private_to_the_authenticated_user(self):
        user_token = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        ).json["access_token"]
        other_token = self.client.post(
            BASE_URL + "/token/", json={"username": "user2", "password": "123"}
        ).json["access_token"]
        user_header = {"Authorization": f"Bearer {user_token}"}
        other_header = {"Authorization": f"Bearer {other_token}"}

        rv = self.client.get(BASE_URL + "/users/-/settings", headers=user_header)
        assert rv.status_code == 200
        assert rv.json == {}

        rv = self.client.put(
            BASE_URL + "/users/-/settings",
            headers=user_header,
            json={"homePerson": "I0042"},
        )
        assert rv.status_code == 200
        assert rv.json == {"homePerson": "I0042"}
        assert self.client.get(
            BASE_URL + "/users/-/settings", headers=user_header
        ).json == {"homePerson": "I0042"}

        rv = self.client.put(
            BASE_URL + "/users/-/settings",
            headers=user_header,
            json={
                "appearance": {
                    "lang": "de",
                    "theme": "dark",
                    "treeDefaultView": "relationship",
                }
            },
        )
        assert rv.status_code == 200
        assert rv.json == {
            "homePerson": "I0042",
            "appearance": {
                "lang": "de",
                "theme": "dark",
                "treeDefaultView": "relationship",
            },
        }
        assert (
            self.client.get(BASE_URL + "/users/-/settings", headers=user_header).json
            == rv.json
        )

        rv = self.client.put(
            BASE_URL + "/users/-/settings",
            headers=user_header,
            json={"appearance": {"theme": "light"}},
        )
        assert rv.status_code == 200
        assert rv.json == {
            "homePerson": "I0042",
            "appearance": {
                "lang": "de",
                "theme": "light",
                "treeDefaultView": "relationship",
            },
        }
        assert (
            self.client.get(BASE_URL + "/users/-/settings", headers=other_header).json
            == {}
        )

    def test_change_password_no_token(self):
        rv = self.client.post(
            BASE_URL + "/users/-/password/change",
            json={"old_password": "123", "new_password": "456"},
        )
        assert rv.status_code == 401

    def test_change_password_wrong_old_pw(self):
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/users/-/password/change",
            headers={"Authorization": f"Bearer {token}"},
            json={"old_password": "012", "new_password": "456"},
        )
        assert rv.status_code == 403

    def test_change_password_empty(self):
        """Test that empty passwords are rejected."""
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/users/-/password/change",
            headers={"Authorization": f"Bearer {token}"},
            json={"old_password": "123", "new_password": ""},
        )
        assert rv.status_code == 400
        # Verify the old password still works
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200

    def test_change_password(self):
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/users/-/password/change",
            headers={"Authorization": f"Bearer {token}"},
            json={"old_password": "123", "new_password": "456"},
        )
        assert rv.status_code == 201
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 403
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "456"}
        )
        assert rv.status_code == 200

    def test_change_other_user_password(self):
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token_user = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        # user can't change owner's PW
        rv = self.client.post(
            BASE_URL + "/users/owner/password/change",
            headers={"Authorization": f"Bearer {token_user}"},
            json={"old_password": "123", "new_password": "456"},
        )
        assert rv.status_code == 403
        # owner can change user's PW
        rv = self.client.post(
            BASE_URL + "/users/user/password/change",
            headers={"Authorization": f"Bearer {token_owner}"},
            json={"old_password": "123", "new_password": "456"},
        )
        assert rv.status_code == 201
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 403
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "456"}
        )
        assert rv.status_code == 200
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        assert rv.status_code == 200

    def test_change_password_twice(self):
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/users/-/password/change",
            headers={"Authorization": f"Bearer {token}"},
            json={"old_password": "123", "new_password": "456"},
        )
        assert rv.status_code == 201
        rv = self.client.post(
            BASE_URL + "/users/-/password/change",
            headers={"Authorization": f"Bearer {token}"},
            json={"old_password": "123", "new_password": "456"},
        )
        assert rv.status_code == 403

    def test_reset_password_trigger_invalid_user(self):
        with patch("gramps_webapi.api.util.smtplib.SMTP_SSL") as mock_smtp:
            mock_smtp_instance = MagicMock()
            mock_smtp.return_value = mock_smtp_instance
            rv = self.client.post(
                BASE_URL + "/users/doesn_exist/password/reset/trigger/"
            )
            assert rv.status_code == 201
            mock_smtp.assert_not_called()

    def test_reset_password_trigger_dash_username(self):
        with patch("gramps_webapi.api.util.smtplib.SMTP_SSL") as mock_smtp:
            rv = self.client.post(BASE_URL + "/users/-/password/reset/trigger/")
            assert rv.status_code == 201
            mock_smtp.assert_not_called()

    def test_reset_password_trigger_no_email(self):
        with self.app.app_context():
            add_user(name="noemail", password="123", role=ROLE_MEMBER, tree=self.tree)
        with patch("gramps_webapi.api.util.smtplib.SMTP_SSL") as mock_smtp:
            rv = self.client.post(BASE_URL + "/users/noemail/password/reset/trigger/")
            assert rv.status_code == 201
            mock_smtp.assert_not_called()

    def test_reset_password_trigger_status(self):
        with patch("gramps_webapi.api.util.smtplib.SMTP_SSL") as mock_smtp:
            mock_smtp_instance = MagicMock()
            mock_smtp.return_value = mock_smtp_instance
            rv = self.client.post(BASE_URL + "/users/user/password/reset/trigger/")
            assert rv.status_code == 201
            mock_smtp_instance.send_message.assert_called_once()

    def test_reset_password(self):
        with patch("gramps_webapi.api.util.smtplib.SMTP_SSL") as mock_smtp:
            mock_smtp_instance = MagicMock()
            mock_smtp.return_value = mock_smtp_instance
            rv = self.client.post(BASE_URL + "/users/user/password/reset/trigger/")
            assert rv.status_code == 201
            mock_smtp_instance.send_message.assert_called_once()
            msg = mock_smtp_instance.send_message.call_args[0][0]
            # extract the token from the message body
            body = msg.get_body().get_payload().replace("=\n", "")
            matches = re.findall(
                r"jwt=3D([a-zA-Z0-9-_]+\.[a-zA-Z0-9-_]+\.[a-zA-Z0-9-_]+)", body
            )
            self.assertEqual(len(matches), 1, msg=body)
            token = matches[0]
            if token[:2] == "3D":
                token = token[2:]
        # try without token!
        rv = self.client.post(
            BASE_URL + "/users/-/password/reset/",
            json={"new_password": "789"},
        )
        self.assertEqual(rv.status_code, 401)
        # try empty PW!
        rv = self.client.post(
            BASE_URL + "/users/-/password/reset/",
            headers={"Authorization": f"Bearer {token}"},
            json={"new_password": ""},
        )
        self.assertEqual(rv.status_code, 400, rv.data)
        # now that should work
        rv = self.client.post(
            BASE_URL + "/users/-/password/reset/",
            headers={"Authorization": f"Bearer {token}"},
            json={"new_password": "789"},
        )
        self.assertEqual(rv.status_code, 201)
        # try again with the same token!
        rv = self.client.post(
            BASE_URL + "/users/-/password/reset/",
            headers={"Authorization": f"Bearer {token}"},
            json={"new_password": "789"},
        )
        self.assertEqual(rv.status_code, 409)
        # old password doesn't work anymore
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 403
        # new password works!
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "789"}
        )
        assert rv.status_code == 200

    def test_show_user(self):
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token_user = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        # user can view themselves
        rv = self.client.get(
            BASE_URL + "/users/-/",
            headers={"Authorization": f"Bearer {token_user}"},
        )
        assert rv.status_code == 200
        self.assertEqual(
            rv.json,
            {
                "name": "user",
                "email": "test@example.com",
                "role": ROLE_MEMBER,
                "full_name": None,
                "tree": self.tree,
            },
        )
        # user cannot view others
        rv = self.client.get(
            BASE_URL + "/users/owner/",
            headers={"Authorization": f"Bearer {token_user}"},
        )
        assert rv.status_code == 403
        # owner can view others
        rv = self.client.get(
            BASE_URL + "/users/user/",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        self.assertEqual(
            rv.json,
            {
                "name": "user",
                "email": "test@example.com",
                "role": ROLE_MEMBER,
                "full_name": None,
                "tree": self.tree,
            },
        )
        # owner cannot view other tree
        rv = self.client.get(
            BASE_URL + "/users/user2/",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 403
        # admin can view other tree
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "admin", "password": "123"},
        )
        assert rv.status_code == 200
        token_admin = rv.json["access_token"]
        rv = self.client.get(
            BASE_URL + "/users/user2/",
            headers={"Authorization": f"Bearer {token_admin}"},
        )
        assert rv.status_code == 200
        self.assertEqual(
            rv.json,
            {
                "name": "user2",
                "email": "test2@example.com",
                "role": ROLE_MEMBER,
                "full_name": None,
                "tree": self.tree2,
            },
        )

    def test_show_users(self):
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token_user = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        # user cannot view users
        rv = self.client.get(
            BASE_URL + "/users/",
            headers={"Authorization": f"Bearer {token_user}"},
        )
        assert rv.status_code == 403
        # owner can view users
        rv = self.client.get(
            BASE_URL + "/users/",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        self.assertEqual(
            set(user["name"] for user in rv.json),
            {"admin", "user", "owner"},
        )

    def test_show_users_filter_by_user_id(self):
        """GET /api/users/?user_id=<id> returns only that user."""
        with self.app.app_context():
            target = user_db.session.query(User).filter_by(name="user").scalar()
            target_id = str(target.id)
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        token_owner = rv.json["access_token"]
        rv = self.client.get(
            BASE_URL + f"/users/?user_id={target_id}",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        assert len(rv.json) == 1
        assert rv.json[0]["name"] == "user"

    def test_show_users_filter_by_unknown_user_id(self):
        """GET /api/users/?user_id=<nonexistent> returns empty list."""
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        token_owner = rv.json["access_token"]
        rv = self.client.get(
            BASE_URL + "/users/?user_id=00000000-0000-0000-0000-000000000000",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        assert rv.json == []

    def test_show_users_filter_by_invalid_user_id_returns_422(self):
        """GET /api/users/?user_id=<invalid> is rejected by UUID validation."""
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        token_owner = rv.json["access_token"]
        rv = self.client.get(
            BASE_URL + "/users/?user_id=not-a-uuid",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 422

    def test_show_users_filter_by_user_id_cross_tree(self):
        """owner of tree1 cannot retrieve a user from tree2 via user_id filter."""
        with self.app.app_context():
            other_tree_user = (
                user_db.session.query(User).filter_by(name="user2").scalar()
            )
            other_id = str(other_tree_user.id)
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        token_owner = rv.json["access_token"]
        rv = self.client.get(
            BASE_URL + f"/users/?user_id={other_id}",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        # user2 belongs to tree2; owner of tree1 must not see them
        assert rv.json == []

    def test_edit_user(self):
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token_user = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        # user can edit themselves
        rv = self.client.put(
            BASE_URL + "/users/-/",
            headers={"Authorization": f"Bearer {token_user}"},
            json={"full_name": "My Name"},
        )
        assert rv.status_code == 200
        rv = self.client.get(
            BASE_URL + "/users/-/",
            headers={"Authorization": f"Bearer {token_user}"},
        )
        assert rv.status_code == 200
        # email is unchanged!
        self.assertEqual(
            rv.json,
            {
                "name": "user",
                "email": "test@example.com",
                "role": ROLE_MEMBER,
                "full_name": "My Name",
                "tree": self.tree,
            },
        )
        # user cannot change others
        rv = self.client.put(
            BASE_URL + "/users/owner/",
            headers={"Authorization": f"Bearer {token_user}"},
            json={"full_name": "My Name"},
        )
        assert rv.status_code == 403
        # owner can edit others
        rv = self.client.put(
            BASE_URL + "/users/user/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json={"full_name": "His Name"},
        )
        assert rv.status_code == 200
        rv = self.client.get(
            BASE_URL + "/users/user/",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        self.assertEqual(
            rv.json,
            {
                "name": "user",
                "email": "test@example.com",
                "role": ROLE_MEMBER,
                "full_name": "His Name",
                "tree": self.tree,
            },
        )

    def test_edit_user_shared_email(self):
        """Two accounts may share an e-mail address, e.g. a family mailbox."""
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        # owner gives user the address owner already uses
        rv = self.client.put(
            BASE_URL + "/users/user/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json={"email": "owner@example.com"},
        )
        assert rv.status_code == 200
        rv = self.client.get(
            BASE_URL + "/users/user/",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        assert rv.json["email"] == "owner@example.com"

    def test_edit_own_user_shared_email(self):
        """A user may set their own address to one another account uses."""
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token_user = rv.json["access_token"]
        rv = self.client.put(
            BASE_URL + "/users/-/",
            headers={"Authorization": f"Bearer {token_user}"},
            json={"email": "owner@example.com"},
        )
        assert rv.status_code == 200
        rv = self.client.get(
            BASE_URL + "/users/-/",
            headers={"Authorization": f"Bearer {token_user}"},
        )
        assert rv.status_code == 200
        assert rv.json["email"] == "owner@example.com"

    def test_add_user(self):
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token_user = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "owner", "password": "123"},
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        # user cannot add user
        rv = self.client.post(
            BASE_URL + "/users/new_user/",
            headers={"Authorization": f"Bearer {token_user}"},
            json={
                "email": "new@example.com",
                "role": ROLE_MEMBER,
                "full_name": "My Name",
                "password": "abc",
            },
        )
        assert rv.status_code == 403
        # missing password
        rv = self.client.post(
            BASE_URL + "/users/new_user/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json={
                "email": "new@example.com",
                "role": ROLE_MEMBER,
                "full_name": "My Name",
            },
        )
        assert rv.status_code == 422
        # existing user
        rv = self.client.post(
            BASE_URL + "/users/user/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json={
                "email": "new@example.com",
                "role": ROLE_MEMBER,
                "full_name": "New Name",
                "password": "abc",
            },
        )
        assert rv.status_code == 409
        assert rv.json["error"]["message"] == "User already exists"
        # OK
        rv = self.client.post(
            BASE_URL + "/users/new_user/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json={
                "email": "new@example.com",
                "role": ROLE_MEMBER,
                "full_name": "New Name",
                "password": "abc",
            },
        )
        assert rv.status_code == 201
        rv = self.client.get(
            BASE_URL + "/users/new_user/",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        # email is unchanged!
        self.assertEqual(
            rv.json,
            {
                "email": "new@example.com",
                "role": ROLE_MEMBER,
                "full_name": "New Name",
                "name": "new_user",
                "tree": self.tree,
            },
        )
        # check token for new user
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "new_user", "password": "abc"}
        )
        assert rv.status_code == 200

    def test_add_user_shared_email(self):
        """A new account may use an address another account already has."""
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/users/new_user/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json={
                "email": "owner@example.com",
                "role": ROLE_MEMBER,
                "full_name": "New Name",
                "password": "abc",
            },
        )
        assert rv.status_code == 201

    def test_bulk_add_users(self):
        """Bulk creation shares addresses, and reports a name clash as 409."""
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "owner", "password": "123"}
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/users/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json=[
                {
                    "name": "bulk1",
                    "email": "family@example.com",
                    "full_name": "Bulk One",
                    "role": ROLE_MEMBER,
                    "tree": self.tree,
                },
                {
                    "name": "bulk2",
                    "email": "family@example.com",
                    "full_name": "Bulk Two",
                    "role": ROLE_MEMBER,
                    "tree": self.tree,
                },
            ],
        )
        assert rv.status_code == 201
        # a username clash is only detected on commit
        rv = self.client.post(
            BASE_URL + "/users/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json=[
                {
                    "name": "bulk1",
                    "email": "other@example.com",
                    "full_name": "Bulk One Again",
                    "role": ROLE_MEMBER,
                    "tree": self.tree,
                }
            ],
        )
        assert rv.status_code == 409

    def test_register_user(self):
        with patch("gramps_webapi.api.util.smtplib.SMTP_SSL") as mock_smtp:
            mock_smtp_instance = MagicMock()
            mock_smtp.return_value = mock_smtp_instance
            # role is not allowed
            rv = self.client.post(
                BASE_URL + "/users/new_user_2/register/",
                json={
                    "email": "new_2@example.com",
                    "role": ROLE_OWNER,
                    "full_name": "My Name",
                    "password": "abc",
                    "tree": self.tree,
                },
            )
            assert rv.status_code == 422
            # missing tree
            rv = self.client.post(
                BASE_URL + "/users/new_user_2/register/",
                json={
                    "email": "new_2@example.com",
                    "full_name": "My Name",
                    "password": "abc",
                },
            )
            # missing password
            rv = self.client.post(
                BASE_URL + "/users/new_user_2/register/",
                json={
                    "email": "new_2@example.com",
                    "full_name": "My Name",
                    "tree": self.tree,
                },
            )
            assert rv.status_code == 422
            # existing user
            rv = self.client.post(
                BASE_URL + "/users/user/register/",
                json={
                    "email": "new_2@example.com",
                    "full_name": "New Name",
                    "password": "abc",
                    "tree": self.tree,
                },
            )
            assert rv.status_code == 409
            assert rv.json["error"]["message"] == "User already exists"
            # OK
            rv = self.client.post(
                BASE_URL + "/users/new_user_2/register/",
                json={
                    "email": "new_2@example.com",
                    "full_name": "New Name",
                    "password": "abc",
                    "tree": self.tree,
                },
            )
            assert rv.status_code == 201
            # get owner token
            rv = self.client.post(
                BASE_URL + "/token/",
                json={"username": "owner", "password": "123"},
            )
            assert rv.status_code == 200
            token_owner = rv.json["access_token"]
            rv = self.client.get(
                BASE_URL + "/users/new_user_2/",
                headers={"Authorization": f"Bearer {token_owner}"},
            )
            assert rv.status_code == 200
            self.assertEqual(
                rv.json,
                {
                    "email": "new_2@example.com",
                    "role": ROLE_UNCONFIRMED,
                    "full_name": "New Name",
                    "name": "new_user_2",
                    "tree": self.tree,
                },
            )
            # new user cannot get token
            rv = self.client.post(
                BASE_URL + "/token/", json={"username": "new_user_2", "password": "abc"}
            )
            assert rv.status_code == 403

    def test_confirm_email(self):
        with patch("gramps_webapi.api.util.smtplib.SMTP_SSL") as mock_smtp:
            mock_smtp_instance = MagicMock()
            mock_smtp.return_value = mock_smtp_instance
            rv = self.client.post(
                BASE_URL + "/users/new_user_3/register/",
                json={
                    "email": "new_3@example.com",
                    "full_name": "New Name",
                    "password": "abc",
                    "tree": self.tree,
                },
            )
            assert rv.status_code == 201
            mock_smtp_instance.send_message.assert_called_once()
            msg = mock_smtp_instance.send_message.call_args[0][0]
            # extract the token from the message body
            body = msg.get_body().get_payload().replace("=\n", "")
            matches = re.findall(
                r"jwt=3D([a-zA-Z0-9-_]+\.[a-zA-Z0-9-_]+\.[a-zA-Z0-9-_]+)", body
            )
            self.assertEqual(len(matches), 1, msg=body)
            token = matches[0]
            if token[:2] == "3D":
                token = token[2:]
            # try without token
            rv = self.client.get(BASE_URL + "/users/-/email/confirm/")
            self.assertEqual(rv.status_code, 401)
            # now that should work
            rv = self.client.get(
                BASE_URL + "/users/-/email/confirm/",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(rv.status_code, 200, rv.data)
            # check return template
            self.assertIn(b"Thank you for confirming your e-mail address", rv.data)
            # get owner token
            rv = self.client.post(
                BASE_URL + "/token/",
                json={"username": "owner", "password": "123"},
            )
            assert rv.status_code == 200
            token_owner = rv.json["access_token"]
            # get user info
            rv = self.client.get(
                BASE_URL + "/users/new_user_3/",
                headers={"Authorization": f"Bearer {token_owner}"},
            )
            assert rv.status_code == 200
            # new role should be ROLE_DISABLED!
            self.assertEqual(
                rv.json,
                {
                    "email": "new_3@example.com",
                    "role": ROLE_DISABLED,
                    "full_name": "New Name",
                    "name": "new_user_3",
                    "tree": self.tree,
                },
            )
            # try getting list of people with email confirmation token
            # this should not be allowed!
            rv = self.client.get(
                BASE_URL + "/people/",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(rv.status_code, 401)

    def test_delete_user(self):
        # get user token
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token_user = rv.json["access_token"]
        # get owner token
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "owner", "password": "123"},
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        # add user
        rv = self.client.post(
            BASE_URL + "/users/user_to_delete/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json={
                "email": "to_delete@example.com",
                "role": ROLE_MEMBER,
                "full_name": "To Delete",
                "password": "abc",
            },
        )
        assert rv.status_code == 201
        rv = self.client.get(
            BASE_URL + "/users/user_to_delete/",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        # check token for new user
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user_to_delete", "password": "abc"}
        )
        assert rv.status_code == 200
        # user cannot delete user
        rv = self.client.delete(
            BASE_URL + "/users/user_to_delete/",
            headers={"Authorization": f"Bearer {token_user}"},
        )
        assert rv.status_code == 403
        # owner can user
        rv = self.client.delete(
            BASE_URL + "/users/user_to_delete/",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        # check user is gone
        rv = self.client.get(
            BASE_URL + "/users/user_to_delete/",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 404
        # check user can't get token
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user_to_delete", "password": "abc"}
        )
        assert rv.status_code == 403

    def test_change_user_role(self):
        # get user token
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "user", "password": "123"}
        )
        assert rv.status_code == 200
        token_user = rv.json["access_token"]
        # get owner token
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "owner", "password": "123"},
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        # get admin token
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "admin", "password": "123"},
        )
        assert rv.status_code == 200
        token_admin = rv.json["access_token"]
        # add user
        rv = self.client.post(
            BASE_URL + "/users/user_change_role/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json={
                "email": "change_role@example.com",
                "role": ROLE_MEMBER,
                "full_name": "Change Role",
                "password": "abc",
            },
        )
        assert rv.status_code == 201
        # get token for new user
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "user_change_role", "password": "abc"},
        )
        assert rv.status_code == 200
        token_new_user = rv.json["access_token"]
        # user can change own details
        rv = self.client.put(
            BASE_URL + "/users/-/",
            headers={"Authorization": "Bearer {}".format(token_new_user)},
            json={"full_name": "Change My Role"},
        )
        assert rv.status_code == 200
        # user cannot change own role
        rv = self.client.put(
            BASE_URL + "/users/-/",
            headers={"Authorization": "Bearer {}".format(token_new_user)},
            json={"role": ROLE_OWNER},
        )
        assert rv.status_code == 403
        # owner can change user role
        rv = self.client.put(
            BASE_URL + "/users/user_change_role/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json={"role": ROLE_OWNER},
        )
        assert rv.status_code == 200
        # owner cannot change user role to admin
        rv = self.client.put(
            BASE_URL + "/users/user_change_role/",
            headers={"Authorization": f"Bearer {token_owner}"},
            json={"role": ROLE_ADMIN},
        )
        assert rv.status_code == 403
        # admin can
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "admin", "password": "123"},
        )
        assert rv.status_code == 200
        token_admin = rv.json["access_token"]
        rv = self.client.put(
            BASE_URL + "/users/user_change_role/",
            headers={"Authorization": f"Bearer {token_admin}"},
            json={"role": ROLE_ADMIN},
        )
        assert rv.status_code == 200

    def test_downgrade_only_owner_forbidden(self):
        """The only owner/admin of a tree cannot be downgraded below owner."""
        # tree2 only has "owner2" with role owner or higher
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "owner2", "password": "123"},
        )
        assert rv.status_code == 200
        token_owner2 = rv.json["access_token"]
        # owner2 cannot downgrade themselves
        rv = self.client.put(
            BASE_URL + "/users/-/",
            headers={"Authorization": f"Bearer {token_owner2}"},
            json={"role": ROLE_MEMBER},
        )
        assert rv.status_code == 405
        assert "only owner" in rv.json["error"]["message"]
        # role is unchanged
        assert get_user_details("owner2")["role"] == ROLE_OWNER
        # get admin token (belongs to self.tree, which has both "owner" and
        # "admin" with role owner or higher)
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "admin", "password": "123"},
        )
        assert rv.status_code == 200
        token_admin = rv.json["access_token"]
        # admin can downgrade "owner" since "admin" remains as owner or higher
        rv = self.client.put(
            BASE_URL + "/users/owner/",
            headers={"Authorization": f"Bearer {token_admin}"},
            json={"role": ROLE_MEMBER},
        )
        assert rv.status_code == 200
        assert get_user_details("owner")["role"] == ROLE_MEMBER
        # now "admin" is the only owner-or-higher user left in self.tree;
        # downgrading them should be forbidden
        rv = self.client.put(
            BASE_URL + "/users/admin/",
            headers={"Authorization": f"Bearer {token_admin}"},
            json={"role": ROLE_MEMBER},
        )
        assert rv.status_code == 405
        assert get_user_details("admin")["role"] == ROLE_ADMIN

    def test_downgrade_only_owner_by_admin_allowed(self):
        """A site admin may downgrade the only owner of another tree."""
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "admin", "password": "123"},
        )
        assert rv.status_code == 200
        token_admin = rv.json["access_token"]
        # "admin" belongs to self.tree, "owner2" is the only owner of tree2
        rv = self.client.put(
            BASE_URL + "/users/owner2/",
            headers={"Authorization": f"Bearer {token_admin}"},
            json={"role": ROLE_MEMBER},
        )
        assert rv.status_code == 200
        assert get_user_details("owner2")["role"] == ROLE_MEMBER

    def test_delete_only_owner_self_forbidden(self):
        """The only owner of a tree cannot delete themselves."""
        # tree2 only has "owner2" with role owner or higher
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "owner2", "password": "123"},
        )
        assert rv.status_code == 200
        token_owner2 = rv.json["access_token"]
        # deleting oneself by name is refused, since nobody would be left
        rv = self.client.delete(
            BASE_URL + "/users/owner2/",
            headers={"Authorization": f"Bearer {token_owner2}"},
        )
        assert rv.status_code == 405
        assert "only owner" in rv.json["error"]["message"]
        assert get_user_details("owner2")["role"] == ROLE_OWNER

    def test_delete_only_owner_by_admin_allowed(self):
        """A site admin may delete the only owner of another tree."""
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "admin", "password": "123"},
        )
        assert rv.status_code == 200
        token_admin = rv.json["access_token"]
        # "admin" belongs to self.tree, "owner2" is the only owner of tree2
        rv = self.client.delete(
            BASE_URL + "/users/owner2/",
            headers={"Authorization": f"Bearer {token_admin}"},
        )
        assert rv.status_code == 200
        assert get_user_details("owner2") is None

    def test_delete_owner_self_allowed_if_others_remain(self):
        """An owner may delete themselves if another owner-or-higher remains."""
        # self.tree has both "owner" and "admin" with role owner or higher
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "owner", "password": "123"},
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        rv = self.client.delete(
            BASE_URL + "/users/owner/",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        assert get_user_details("owner") is None

    def test_add_users(self):
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "owner", "password": "123"},
        )
        assert rv.status_code == 200
        token_owner = rv.json["access_token"]
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "admin", "password": "123"},
        )
        assert rv.status_code == 200
        token_admin = rv.json["access_token"]
        # other tree - not allowed
        users = [{"name": "new_user_1", "tree": self.tree2}]
        rv = self.client.post(
            BASE_URL + "/users/",
            json=users,
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 403
        users = [{"name": "new_user_1", "tree": "not_exists"}]
        rv = self.client.post(
            BASE_URL + "/users/",
            json=users,
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 422
        # OK - same tree
        users = [{"name": "new_user_1"}]
        rv = self.client.post(
            BASE_URL + "/users/",
            json=users,
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 201
        rv = self.client.get(
            BASE_URL + "/users/new_user_1/",
            headers={"Authorization": f"Bearer {token_owner}"},
        )
        assert rv.status_code == 200
        assert rv.json["tree"] == self.tree


class TestUserCreateOwner(unittest.TestCase):
    """Test cases for the /api/user/create_owner endpoint."""

    def setUp(self):
        self.name = "Test Web API"
        self.dbman = CLIDbManager(DbState())
        _, _name = self.dbman.create_new_db_cli(self.name, dbid="sqlite")
        with patch.dict("os.environ", {ENV_CONFIG_FILE: TEST_AUTH_CONFIG}):
            self.app = create_app(
                config={"TESTING": True, "RATELIMIT_ENABLED": False},
                config_from_env=False,
            )
        self.client = self.app.test_client()
        with self.app.app_context():
            user_db.create_all()
            db_manager = WebDbManager(name=self.name, create_if_missing=False)
            self.tree = db_manager.dirname
        self.ctx = self.app.test_request_context()
        self.ctx.push()

    def _delete_users(self):
        """Delete existing users."""
        users = get_all_user_details(tree=None)
        for user in users:
            delete_user(name=user["name"])

    def tearDown(self):
        self.ctx.pop()
        self.dbman.remove_database(self.name)

    def test_create_admin(self):
        self._delete_users()
        rv = self.client.get(f"{BASE_URL}/token/create_owner/")
        assert rv.status_code == 200
        token = rv.json["access_token"]
        with self.app.app_context():
            assert get_number_users() == 0
        # data missing
        rv = self.client.post(
            f"{BASE_URL}/users/site_admin/create_owner/",
            headers={"Authorization": f"Bearer {token}"},
            json={"full_name": "My Name"},
        )
        assert rv.status_code == 422
        with self.app.app_context():
            assert get_number_users() == 0
        # non-existing tree
        rv = self.client.post(
            f"{BASE_URL}/users/site_admin/create_owner/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "password": "123",
                "email": "test@example.com",
                "full_name": "My Name",
                "tree": "some_tree",
            },
        )
        assert rv.status_code == 422
        with self.app.app_context():
            assert get_number_users() == 0
        rv = self.client.post(
            f"{BASE_URL}/users/site_admin/create_owner/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "password": "123",
                "email": "test@example.com",
                "full_name": "My Name",
            },
        )
        assert rv.status_code == 201
        with self.app.app_context():
            assert get_number_users() == 1
            assert get_user_details("site_admin")["role"] == ROLE_ADMIN
        # try posting again
        rv = self.client.post(
            f"{BASE_URL}/users/site_admin_2/create_owner/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "password": "123",
                "email": "test@example.com",
                "full_name": "My Name",
            },
        )
        assert rv.status_code == 405
        with self.app.app_context():
            assert get_number_users() == 1
        rv = self.client.get(f"{BASE_URL}/token/create_owner/")
        assert rv.status_code == 405

    def test_create_owner(self):
        self._delete_users()
        rv = self.client.post(
            f"{BASE_URL}/token/create_owner/", json={"tree": self.tree}
        )
        assert rv.status_code == 201
        token = rv.json["access_token"]
        with self.app.app_context():
            assert get_number_users() == 0
        # data missing
        rv = self.client.post(
            f"{BASE_URL}/users/tree_owner/create_owner/",
            headers={"Authorization": f"Bearer {token}"},
            json={"full_name": "My Name"},
        )
        assert rv.status_code == 422
        with self.app.app_context():
            assert get_number_users() == 0
        # non-existing tree
        rv = self.client.post(
            f"{BASE_URL}/users/tree_owner/create_owner/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "password": "123",
                "email": "test@example.com",
                "full_name": "My Name",
                "tree": "some_tree",
            },
        )
        assert rv.status_code == 422
        with self.app.app_context():
            assert get_number_users() == 0
        rv = self.client.post(
            f"{BASE_URL}/users/tree_owner/create_owner/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "password": "123",
                "email": "test@example.com",
                "full_name": "My Name",
            },
        )
        assert rv.status_code == 201
        with self.app.app_context():
            assert get_number_users() == 1
            assert get_user_details("tree_owner")["role"] == ROLE_OWNER
            assert get_user_details("tree_owner")["tree"] == self.tree
        # try posting again
        rv = self.client.post(
            f"{BASE_URL}/users/tree_owner_2/create_owner/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "password": "123",
                "email": "test@example.com",
                "full_name": "My Name",
            },
        )
        assert rv.status_code == 405
        with self.app.app_context():
            assert get_number_users() == 1
        rv = self.client.get(f"{BASE_URL}/token/create_owner/")
        assert rv.status_code == 405


class TestUserNameChange(unittest.TestCase):
    """Test cases for changing user names."""

    def setUp(self):
        self.name = "Test Web API"
        self.dbman = CLIDbManager(DbState())
        dbpath, _ = self.dbman.create_new_db_cli(self.name, dbid="sqlite")
        self.tree = os.path.basename(dbpath)
        with patch.dict("os.environ", {ENV_CONFIG_FILE: TEST_AUTH_CONFIG}):
            self.app = create_app(
                config={"TESTING": True, "RATELIMIT_ENABLED": False},
                config_from_env=False,
            )
        self.client = self.app.test_client()
        with self.app.app_context():
            user_db.create_all()
            add_user(
                name="admin",
                password="123",
                email="admin@example.com",
                role=ROLE_ADMIN,
                tree=self.tree,
            )
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        # Get admin token
        rv = self.client.post(
            BASE_URL + "/token/", json={"username": "admin", "password": "123"}
        )
        self.token = rv.json["access_token"]

    def tearDown(self):
        self.ctx.pop()
        self.dbman.remove_database(self.name)

    def test_change_own_username(self):
        """Test changing own username."""
        # Create a user
        with self.app.app_context():
            add_user(
                name="testuser",
                password="testpass",
                email="test@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )

        # Login as the user
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "testuser", "password": "testpass"},
        )
        assert rv.status_code == 200
        token = rv.json["access_token"]

        # Change own username using "-"
        rv = self.client.put(
            BASE_URL + "/users/-/",
            headers={"Authorization": f"Bearer {token}"},
            json={"name_new": "renameduser"},
        )
        assert rv.status_code == 200

        # Verify old username doesn't work
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "testuser", "password": "testpass"},
        )
        assert rv.status_code == 403

        # Verify new username works
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "renameduser", "password": "testpass"},
        )
        assert rv.status_code == 200

        # Verify user details reflect new name using - (self-reference)
        rv = self.client.get(
            BASE_URL + "/users/-/",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert rv.status_code == 200
        assert rv.json["name"] == "renameduser"

    def test_admin_change_other_username(self):
        """Test admin changing another user's username."""
        # Create a regular user
        with self.app.app_context():
            add_user(
                name="regularuser",
                password="pass123",
                email="regular@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )

        # Admin changes the username
        rv = self.client.put(
            BASE_URL + "/users/regularuser/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"name_new": "renamedregular"},
        )
        assert rv.status_code == 200

        # Verify old username doesn't exist
        rv = self.client.get(
            BASE_URL + "/users/regularuser/",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        assert rv.status_code == 404

        # Verify new username exists
        rv = self.client.get(
            BASE_URL + "/users/renamedregular/",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        assert rv.status_code == 200
        assert rv.json["name"] == "renamedregular"

    def test_change_username_duplicate(self):
        """Test that changing to an existing username fails."""
        # Create two users
        with self.app.app_context():
            add_user(
                name="user1",
                password="pass1",
                email="user1@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )
            add_user(
                name="user2",
                password="pass2",
                email="user2@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )

        # Try to rename user1 to user2
        rv = self.client.put(
            BASE_URL + "/users/user1/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"name_new": "user2"},
        )
        assert rv.status_code == 409
        assert "already exists" in rv.json["error"]["message"].lower()

    def test_change_username_empty(self):
        """Test that changing to empty username fails."""
        # Create a user
        with self.app.app_context():
            add_user(
                name="testuser",
                password="testpass",
                email="test@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )

        # Try to rename to empty string
        rv = self.client.put(
            BASE_URL + "/users/testuser/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"name_new": ""},
        )
        assert rv.status_code == 400
        assert "empty" in rv.json["error"]["message"].lower()

        # Try to rename to whitespace
        rv = self.client.put(
            BASE_URL + "/users/testuser/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"name_new": "   "},
        )
        assert rv.status_code == 400
        assert "empty" in rv.json["error"]["message"].lower()

    def test_change_username_reserved(self):
        """Test that changing to reserved usernames fails."""
        # Create a user
        with self.app.app_context():
            add_user(
                name="testuser",
                password="testpass",
                email="test@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )

        # Try to rename to "-"
        rv = self.client.put(
            BASE_URL + "/users/testuser/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"name_new": "-"},
        )
        assert rv.status_code == 400
        assert "reserved" in rv.json["error"]["message"].lower()

        # Try to rename to "_"
        rv = self.client.put(
            BASE_URL + "/users/testuser/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"name_new": "_"},
        )
        assert rv.status_code == 400
        assert "reserved" in rv.json["error"]["message"].lower()

    def test_change_username_token_still_valid(self):
        """Test that JWT tokens remain valid after username change."""
        # Create a user
        with self.app.app_context():
            add_user(
                name="testuser",
                password="testpass",
                email="test@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )

        # Login and get token
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "testuser", "password": "testpass"},
        )
        assert rv.status_code == 200
        user_token = rv.json["access_token"]

        # Change username using the token
        rv = self.client.put(
            BASE_URL + "/users/-/",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"name_new": "renameduser"},
        )
        assert rv.status_code == 200

        # Verify the old token still works (it has user_id, not username)
        rv = self.client.get(
            BASE_URL + "/users/-/",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert rv.status_code == 200
        assert rv.json["name"] == "renameduser"

    def test_change_username_permission_denied(self):
        """Test that non-admin cannot change other user's username."""
        # Create two regular users
        with self.app.app_context():
            add_user(
                name="user1",
                password="pass1",
                email="user1@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )
            add_user(
                name="user2",
                password="pass2",
                email="user2@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )

        # Login as user1
        rv = self.client.post(
            BASE_URL + "/token/",
            json={"username": "user1", "password": "pass1"},
        )
        assert rv.status_code == 200
        token = rv.json["access_token"]

        # Try to change user2's username
        rv = self.client.put(
            BASE_URL + "/users/user2/",
            headers={"Authorization": f"Bearer {token}"},
            json={"name_new": "renameduser2"},
        )
        assert rv.status_code == 403

    def test_change_username_combined_with_other_fields(self):
        """Test that username can be changed along with other fields."""
        # Create a user
        with self.app.app_context():
            add_user(
                name="testuser",
                password="testpass",
                email="old@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )

        # Change username and email together
        rv = self.client.put(
            BASE_URL + "/users/testuser/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "name_new": "newusername",
                "email": "new@example.com",
                "full_name": "New Full Name",
            },
        )
        assert rv.status_code == 200

        # Verify all changes
        rv = self.client.get(
            BASE_URL + "/users/newusername/",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        assert rv.status_code == 200
        assert rv.json["name"] == "newusername"
        assert rv.json["email"] == "new@example.com"
        assert rv.json["full_name"] == "New Full Name"

    def test_change_username_to_same_name(self):
        """Test that changing username to the same name succeeds (no-op)."""
        # Create a user
        with self.app.app_context():
            add_user(
                name="testuser",
                password="testpass",
                email="test@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )

        # Change username to the same name
        rv = self.client.put(
            BASE_URL + "/users/testuser/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"name_new": "testuser"},
        )
        assert rv.status_code == 200

        # Verify user still exists
        rv = self.client.get(
            BASE_URL + "/users/testuser/",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        assert rv.status_code == 200
        assert rv.json["name"] == "testuser"

    def test_change_username_oidc_account_preserved(self):
        """Test that OIDC account associations are preserved after username change."""
        # Create a user
        with self.app.app_context():
            add_user(
                name="oidcuser",
                password="testpass",
                email="oidc@example.com",
                role=ROLE_MEMBER,
                tree=self.tree,
            )
            # Get user GUID and create OIDC association
            user_id = get_guid("oidcuser")
            create_oidc_account(
                user_id=user_id,
                provider_id="google",
                subject_id="google-user-12345",
                email="oidc@example.com",
            )
            # Verify OIDC account exists before rename
            oidc_accounts_before = get_user_oidc_accounts(user_id)
            assert len(oidc_accounts_before) == 1
            assert oidc_accounts_before[0]["provider_id"] == "google"
            assert oidc_accounts_before[0]["subject_id"] == "google-user-12345"

        # Change username
        rv = self.client.put(
            BASE_URL + "/users/oidcuser/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"name_new": "renamedoidcuser"},
        )
        assert rv.status_code == 200

        # Verify the username changed
        rv = self.client.get(
            BASE_URL + "/users/renamedoidcuser/",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        assert rv.status_code == 200
        assert rv.json["name"] == "renamedoidcuser"

        # Verify in database that the OIDC link still exists and points to the same user_id
        with self.app.app_context():
            new_user_id = get_guid("renamedoidcuser")
            # User ID should remain the same (GUIDs don't change)
            assert new_user_id == user_id
            # OIDC account associations should be preserved
            oidc_accounts_after = get_user_oidc_accounts(new_user_id)
            assert len(oidc_accounts_after) == 1
            assert oidc_accounts_after[0]["provider_id"] == "google"
            assert oidc_accounts_after[0]["subject_id"] == "google-user-12345"
