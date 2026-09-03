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

"""Server configuration resources."""

import smtplib

from flask import abort, jsonify
from marshmallow import Schema
from webargs import fields, validate

from ...auth import config_delete, config_get, config_get_all, config_set
from ...auth.const import PERM_EDIT_SETTINGS, PERM_VIEW_SETTINGS
from ...const import DB_CONFIG_ALLOWED_KEYS
from ..auth import require_permissions
from ..blueprint import api_blueprint
from ..util import _resolve_smtp_config, get_config, send_email
from . import ProtectedResource


class ConfigsResource(ProtectedResource):
    """Resource for configuration settings."""

    def get(self):
        """Get all config settings."""
        require_permissions([PERM_VIEW_SETTINGS])
        return jsonify(config_get_all()), 200


class ConfigValueArgs(Schema):
    """Request body for PUT /config/<key>/."""

    value = fields.Str(
        required=True,
        metadata={"description": "The new value for the configuration setting."},
    )


class ConfigResource(ProtectedResource):
    """Resource for a single config setting."""

    def get(self, key: str):
        """Get a config setting."""
        require_permissions([PERM_VIEW_SETTINGS])
        if key not in DB_CONFIG_ALLOWED_KEYS:
            abort(404)
        val = config_get(key)
        if val is None:
            abort(404)
        return jsonify(val), 200

    @api_blueprint.arguments(ConfigValueArgs, location="json")
    def put(self, args, key: str):
        """Update a config setting."""
        require_permissions([PERM_EDIT_SETTINGS])
        try:
            config_set(key=key, value=args["value"])
        except ValueError:
            abort(404)  # key not allowed
        return "", 200

    def delete(self, key: str):
        """Delete a config setting."""
        require_permissions([PERM_EDIT_SETTINGS])
        try:
            if config_get(key=key) is None:
                abort(404)
        except ValueError:
            abort(404)
        config_delete(key=key)
        return "", 200


class EmailConfigArgs(Schema):
    """Request body for PUT /config/email/."""

    host = fields.Str(required=True)
    port = fields.Int(required=True, validate=validate.Range(min=1, max=65535))
    username = fields.Str(required=True)
    password = fields.Str(load_default=None, allow_none=True)
    from_email = fields.Str(required=True)
    security = fields.Str(
        required=True,
        validate=validate.OneOf(["ssl", "starttls", "none"]),
    )


class EmailTestArgs(Schema):
    """Request body for POST /config/email/test/."""

    recipient = fields.Email(required=True)


def _get_email_config() -> dict:
    """Return the effective email configuration without its password."""
    port = int(get_config("EMAIL_PORT"))
    use_ssl, use_starttls = _resolve_smtp_config(
        get_config("EMAIL_USE_SSL"),
        get_config("EMAIL_USE_STARTTLS"),
        get_config("EMAIL_USE_TLS"),
        port,
    )
    security = "ssl" if use_ssl else "starttls" if use_starttls else "none"
    return {
        "host": get_config("EMAIL_HOST") or "",
        "port": port,
        "username": get_config("EMAIL_HOST_USER") or "",
        "from_email": get_config("DEFAULT_FROM_EMAIL") or "",
        "security": security,
        "password_set": bool(get_config("EMAIL_HOST_PASSWORD")),
    }


class EmailConfigResource(ProtectedResource):
    """Administration resource for SMTP settings."""

    def get(self):
        """Get the effective SMTP settings without returning the password."""
        require_permissions([PERM_VIEW_SETTINGS])
        return jsonify(_get_email_config()), 200

    @api_blueprint.arguments(EmailConfigArgs, location="json")
    def put(self, args):
        """Update the persisted SMTP settings."""
        require_permissions([PERM_EDIT_SETTINGS])
        config_set("EMAIL_HOST", args["host"])
        config_set("EMAIL_PORT", str(args["port"]))
        config_set("EMAIL_HOST_USER", args["username"])
        config_set("DEFAULT_FROM_EMAIL", args["from_email"])
        config_set("EMAIL_USE_SSL", str(args["security"] == "ssl").lower())
        config_set("EMAIL_USE_STARTTLS", str(args["security"] == "starttls").lower())
        if args["password"] is not None:
            config_set("EMAIL_HOST_PASSWORD", args["password"])
        elif not args["username"]:
            config_set("EMAIL_HOST_PASSWORD", "")
        return jsonify(_get_email_config()), 200


class EmailTestResource(ProtectedResource):
    """Administration resource for testing the SMTP configuration."""

    @api_blueprint.arguments(EmailTestArgs, location="json")
    def post(self, args):
        """Send a test email using the current SMTP settings."""
        require_permissions([PERM_EDIT_SETTINGS])
        try:
            send_email(
                subject="Gramps Web test email",
                body=(
                    "This is a test email sent from the Gramps Web "
                    "administration settings."
                ),
                to=[args["recipient"]],
            )
        except (ValueError, smtplib.SMTPException) as error:
            abort(502, description=str(error))
        return jsonify({"message": "Test email sent."}), 200
