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

"""Resources for user-managed, expiring full-API keys."""

from flask import abort
from flask_jwt_extended import get_jwt, get_jwt_identity
from marshmallow import Schema
from webargs import fields, validate

from ...auth import (
    create_user_api_key,
    get_name,
    list_user_api_keys,
    revoke_user_api_key,
)
from ...auth.const import PERM_EDIT_OWN_USER
from ..auth import require_permissions
from ..blueprint import api_blueprint
from ..util import abort_with_message, get_tree_from_jwt_or_fail
from . import ProtectedResource


class ApiKeySchema(Schema):
    """Public metadata for one API key."""

    id = fields.Str(required=True)
    name = fields.Str(required=True)
    fingerprint = fields.Str(required=True)
    created_at = fields.DateTime(required=True)
    expires_at = fields.DateTime(required=True)
    expires_on = fields.Date(required=True)


class ApiKeyCreateSchema(Schema):
    """Request body for creating an API key."""

    name = fields.Str(required=True, validate=validate.Length(min=1, max=128))
    expires_on = fields.Date(required=True)


class ApiKeyCreatedSchema(Schema):
    """One-time response containing the new key value."""

    api_key = fields.Nested(ApiKeySchema, required=True)
    token = fields.Str(required=True)


class UserApiKeysResource(ProtectedResource):
    """List and create the current user's API keys."""

    def _get_user_name(self) -> str:
        if get_jwt().get("api_key_id"):
            abort(403)
        require_permissions([PERM_EDIT_OWN_USER])
        get_tree_from_jwt_or_fail()
        try:
            return get_name(get_jwt_identity())
        except ValueError:
            abort_with_message(401, "User not found for token ID")
            raise  # unreachable

    @api_blueprint.response(200, ApiKeySchema(many=True))
    def get(self):
        """List non-revoked API keys for the current user."""
        return list_user_api_keys(self._get_user_name()), 200

    @api_blueprint.response(201, ApiKeyCreatedSchema())
    @api_blueprint.arguments(ApiKeyCreateSchema, location="json")
    def post(self, args):
        """Create an API key, returning its credential exactly once."""
        try:
            api_key, token = create_user_api_key(
                username=self._get_user_name(),
                name=args["name"],
                expires_on=args["expires_on"],
            )
        except ValueError as exc:
            abort_with_message(422, str(exc))
            raise  # unreachable
        return {"api_key": api_key, "token": token}, 201


class UserApiKeyResource(ProtectedResource):
    """Revoke one of the current user's API keys."""

    def delete(self, api_key_id: str):
        """Revoke an API key immediately."""
        if get_jwt().get("api_key_id"):
            abort(403)
        require_permissions([PERM_EDIT_OWN_USER])
        get_tree_from_jwt_or_fail()
        try:
            username = get_name(get_jwt_identity())
        except ValueError:
            abort_with_message(401, "User not found for token ID")
            raise  # unreachable
        if not revoke_user_api_key(username, api_key_id):
            abort(404)
        return "", 204
