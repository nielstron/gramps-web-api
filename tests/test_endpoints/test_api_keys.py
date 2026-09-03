#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2026      Gramps Web contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#

"""Tests for full-API keys with user-selected expiry dates."""

import datetime
import unittest

from gramps_webapi.auth import modify_user
from gramps_webapi.auth.const import ROLE_DISABLED, ROLE_OWNER

from . import BASE_URL, get_test_client
from .util import fetch_header

API_KEYS_URL = BASE_URL + "/users/-/api-keys/"


class TestApiKeys(unittest.TestCase):
    """Test API-key creation, use, listing, expiry, and revocation."""

    @classmethod
    def setUpClass(cls):
        cls.client = get_test_client()

    def setUp(self):
        self.header = fetch_header(self.client, role=ROLE_OWNER)

    def tearDown(self):
        modify_user("owner", role=ROLE_OWNER)

    def _create_key(self, name="Automation", days=30):
        expires_on = datetime.date.today() + datetime.timedelta(days=days)
        return self.client.post(
            API_KEYS_URL,
            json={"name": name, "expires_on": expires_on.isoformat()},
            headers=self.header,
        )

    def test_api_key_lifecycle_and_full_api_access(self):
        rv = self._create_key()
        self.assertEqual(rv.status_code, 201)
        token = rv.json["token"]
        api_key = rv.json["api_key"]
        key_id = api_key["id"]
        self.assertTrue(token.startswith("eyJ"))
        self.assertEqual(api_key["name"], "Automation")
        self.assertEqual(
            api_key["expires_on"],
            (datetime.date.today() + datetime.timedelta(days=30)).isoformat(),
        )
        self.assertNotIn("token", api_key)

        key_header = {"Authorization": f"Bearer {token}"}
        rv = self.client.get(BASE_URL + "/people/?pagesize=1", headers=key_header)
        self.assertEqual(rv.status_code, 200)

        rv = self.client.get(API_KEYS_URL, headers=self.header)
        self.assertEqual(rv.status_code, 200)
        matching = [item for item in rv.json if item["id"] == key_id]
        self.assertEqual(len(matching), 1)
        self.assertNotIn("token", matching[0])

        rv = self.client.delete(API_KEYS_URL + f"{key_id}/", headers=self.header)
        self.assertEqual(rv.status_code, 204)
        rv = self.client.get(BASE_URL + "/people/?pagesize=1", headers=key_header)
        self.assertEqual(rv.status_code, 401)

    def test_multiple_keys_are_independent(self):
        first_response = self._create_key(name="First").json
        second_response = self._create_key(name="Second").json
        first = first_response["api_key"]
        second = second_response["api_key"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first_response["token"], second_response["token"])

        rv = self.client.delete(API_KEYS_URL + f"{first['id']}/", headers=self.header)
        self.assertEqual(rv.status_code, 204)
        second_header = {"Authorization": f"Bearer {second_response['token']}"}
        rv = self.client.get(BASE_URL + "/people/?pagesize=1", headers=second_header)
        self.assertEqual(rv.status_code, 200)

    def test_rejects_expired_or_empty_key_request(self):
        rv = self._create_key(days=-1)
        self.assertEqual(rv.status_code, 422)
        rv = self.client.post(
            API_KEYS_URL,
            json={
                "name": "",
                "expires_on": (
                    datetime.date.today() + datetime.timedelta(days=1)
                ).isoformat(),
            },
            headers=self.header,
        )
        self.assertEqual(rv.status_code, 422)

    def test_api_key_cannot_manage_api_keys(self):
        created = self._create_key().json
        key_header = {"Authorization": f"Bearer {created['token']}"}
        rv = self.client.get(API_KEYS_URL, headers=key_header)
        self.assertEqual(rv.status_code, 403)

    def test_disabled_user_api_key_is_rejected(self):
        created = self._create_key().json
        modify_user("owner", role=ROLE_DISABLED)
        key_header = {"Authorization": f"Bearer {created['token']}"}
        rv = self.client.get(BASE_URL + "/people/?pagesize=1", headers=key_header)
        self.assertEqual(rv.status_code, 401)
