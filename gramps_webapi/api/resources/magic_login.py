"""Passwordless sign-in links sent by e-mail."""

import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import sqlalchemy as sa
from flask import jsonify, render_template
from marshmallow import Schema, fields

from ...auth import AccessToken, User, user_db
from ...auth.const import ROLE_GUEST
from ..blueprint import api_blueprint
from ..ratelimiter import limiter
from ..tasks import run_task, send_email_magic_login
from ..util import abort_with_message, get_config
from . import Resource
from .token import get_tokens, get_tree_id_and_permissions

MAGIC_LOGIN_SCOPE = "magic_login"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _find_user(email: str):
    """Find the active account whose username is its e-mail address."""
    normalized = email.strip().casefold()
    query = user_db.session.query(User).filter(User.role >= ROLE_GUEST)
    exact_username = query.filter(sa.func.lower(User.name) == normalized).all()
    if len(exact_username) == 1:
        return exact_username[0]
    matching_email = query.filter(sa.func.lower(User.email) == normalized).all()
    return matching_email[0] if len(matching_email) == 1 else None


def _issue_token(user) -> str:
    """Rotate the stored one-time secret for a user."""
    token = secrets.token_urlsafe(32)
    token_hash = sha256(token.encode()).hexdigest()
    row = (
        user_db.session.query(AccessToken)
        .filter_by(user_id=user.id, scope=MAGIC_LOGIN_SCOPE)
        .scalar()
    )
    if row is None:
        row = AccessToken(user_id=user.id, scope=MAGIC_LOGIN_SCOPE)
        user_db.session.add(row)
    row.token_hash = token_hash
    row.revoked_at = None
    row.updated_at = _now()
    user_db.session.commit()
    return token


def _consume_token(token: str):
    """Atomically consume a valid token and return its user."""
    token_hash = sha256(token.encode()).hexdigest()
    row = (
        user_db.session.query(AccessToken)
        .filter_by(scope=MAGIC_LOGIN_SCOPE, token_hash=token_hash, revoked_at=None)
        .scalar()
    )
    if row is None or row.updated_at < _now() - timedelta(minutes=15):
        abort_with_message(403, "This sign-in link is invalid or has expired")
    user = user_db.session.get(User, row.user_id)
    consumed = user_db.session.execute(
        sa.update(AccessToken)
        .where(AccessToken.id == row.id, AccessToken.token_hash == token_hash)
        .values(
            token_hash=None,
            revoked_at=_now(),
            updated_at=_now(),
        )
    )
    if consumed.rowcount != 1:
        user_db.session.rollback()
        abort_with_message(409, "This sign-in link has already been used")
    user_db.session.commit()
    if user is None or user.role < ROLE_GUEST:
        abort_with_message(403, "This account cannot sign in")
    return user


class MagicLoginRequestArgs(Schema):
    """Request a link for an e-mail address."""

    email = fields.Email(required=True)


class MagicLoginConsumeArgs(Schema):
    """Consume the opaque token from the e-mail link."""

    token = fields.String(required=True)


class MagicLoginResource(Resource):
    """Send a magic link without revealing whether an account exists."""

    @limiter.limit("3/hour")
    @api_blueprint.arguments(MagicLoginRequestArgs, location="json")
    def post(self, args):
        user = _find_user(args["email"])
        if user is not None and user.email:
            token = _issue_token(user)
            run_task(send_email_magic_login, email=user.email, token=token)
        return "", 201


class MagicLoginConsumeResource(Resource):
    """Browser landing page and one-time token exchange."""

    def get(self):
        return (
            render_template(
                "magic_login.html",
                login_url=get_config("BASE_URL").rstrip("/") + "/",
            ),
            200,
            {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"},
        )

    @limiter.limit("5/minute")
    @api_blueprint.arguments(MagicLoginConsumeArgs, location="json")
    def post(self, args):
        user = _consume_token(args["token"])
        tree_id, permissions = get_tree_id_and_permissions(
            user_id=str(user.id), username=user.name
        )
        return jsonify(
            get_tokens(
                user_id=str(user.id),
                permissions=permissions,
                tree_id=tree_id,
                include_refresh=True,
                fresh=True,
            )
        )
