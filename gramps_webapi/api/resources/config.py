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
        return (
            jsonify(
                {
                    key: value
                    for key, value in config_get_all().items()
                    if key != "AI_SETTINGS"
                }
            ),
            200,
        )


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
        if key == "AI_SETTINGS":
            abort(404)
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
        if key == "AI_SETTINGS":
            abort(404)
        try:
            config_set(key=key, value=args["value"])
        except ValueError:
            abort(404)  # key not allowed
        return "", 200

    def delete(self, key: str):
        """Delete a config setting."""
        require_permissions([PERM_EDIT_SETTINGS])
        if key == "AI_SETTINGS":
            abort(404)
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
    from_name = fields.Str(required=True)
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
        "from_name": get_config("DEFAULT_FROM_NAME") or "",
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
        config_set("DEFAULT_FROM_NAME", args["from_name"])
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


class AiConfigArgs(Schema):
    """OpenAI-compatible chat and local or remote embedding settings."""

    enabled = fields.Bool(required=True)
    chat_model = fields.Str(required=True, validate=validate.Length(max=300))
    chat_base_url = fields.Str(required=True, validate=validate.Length(max=2048))
    chat_api_key = fields.Str(
        load_default=None, allow_none=True, validate=validate.Length(max=4096)
    )
    embedding_model = fields.Str(required=True, validate=validate.Length(max=300))
    embedding_base_url = fields.Str(required=True, validate=validate.Length(max=2048))
    embedding_api_key = fields.Str(
        load_default=None, allow_none=True, validate=validate.Length(max=4096)
    )


AI_FIELDS = {
    "enabled": "AI_ENABLED",
    "chat_model": "LLM_MODEL",
    "chat_base_url": "LLM_BASE_URL",
    "chat_api_key": "LLM_API_KEY",
    "embedding_model": "VECTOR_EMBEDDING_MODEL",
    "embedding_base_url": "VECTOR_EMBEDDING_BASE_URL",
    "embedding_api_key": "VECTOR_EMBEDDING_API_KEY",
}


def _get_ai_settings():
    from ...ai_config import get_ai_config

    config = get_ai_config()
    result = {}
    for field, key in AI_FIELDS.items():
        if field.endswith("api_key"):
            result[f"{field}_set"] = bool(config[key])
        else:
            result[field] = config[key] if field == "enabled" else config[key] or ""
    result["enabled"] = bool(
        result["enabled"] and result["chat_model"] and result["embedding_model"]
    )
    return result


class AiConfigResource(ProtectedResource):
    """Server-wide AI settings; credentials are write-only."""

    def get(self):
        require_permissions([PERM_VIEW_SETTINGS])
        return jsonify(_get_ai_settings()), 200

    @api_blueprint.arguments(AiConfigArgs, location="json")
    def put(self, args):
        import json
        from urllib.parse import urlsplit

        from ...ai_config import get_ai_config

        require_permissions([PERM_EDIT_SETTINGS])
        for field in ("chat_base_url", "embedding_base_url"):
            if args[field]:
                url = urlsplit(args[field])
                if (
                    url.scheme not in {"http", "https"}
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.query
                    or url.fragment
                ):
                    abort(
                        422,
                        description="Use an HTTP(S) base URL without credentials, query parameters, or fragments.",
                    )
        if args["enabled"] and (
            not args["chat_model"].strip() or not args["embedding_model"].strip()
        ):
            abort(
                422, description="Chat and embedding models are required to enable AI."
            )
        config = get_ai_config()
        for field, key in AI_FIELDS.items():
            if args[field] is not None:
                config[key] = (
                    args[field].strip() if isinstance(args[field], str) else args[field]
                )
        # One write makes model, endpoint and credential changes atomic across workers.
        config_set("AI_SETTINGS", json.dumps(config))
        return jsonify(_get_ai_settings()), 200
