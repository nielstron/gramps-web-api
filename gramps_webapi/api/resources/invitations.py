"""Owner-issued invitations, independent of public registration."""

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import sqlalchemy as sa
from flask import current_app, jsonify, render_template
from flask_jwt_extended import create_access_token, get_jwt, get_jwt_identity
from marshmallow import Schema, fields, validate
from sqlalchemy.exc import IntegrityError

from ...auth import User, UserInvitation, user_db
from ...auth.const import (
    CLAIM_LIMITED_SCOPE,
    PERM_ADD_OTHER_TREE_USER,
    PERM_ADD_USER,
    PERM_DEL_OTHER_TREE_USER,
    PERM_DEL_USER,
    PERM_MAKE_ADMIN,
    PERM_VIEW_OTHER_TREE_USER,
    PERM_VIEW_OTHER_USER,
    ROLE_ADMIN,
    SCOPE_ACCEPT_INVITATION,
)
from ...auth.passwords import hash_password
from ...const import TREE_MULTI
from ..auth import has_permissions, require_permissions
from ..blueprint import api_blueprint
from ..ratelimiter import limiter
from ..tasks import run_task, send_email_invitation
from ..util import abort_with_message, get_config, get_tree_from_jwt, tree_exists
from . import LimitedScopeProtectedResource, ProtectedResource


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _details(invitation):
    return {
        "id": invitation.id,
        "email": invitation.email,
        "role": invitation.role,
        "tree": invitation.tree,
        "expires_at": invitation.expires_at.isoformat() + "Z",
    }


def _existing_email(email, tree):
    return (
        user_db.session.query(User)
        .filter(sa.func.lower(User.email) == email.lower(), User.tree == tree)
        .first()
        is not None
    )


def _send_invitation(invitation):
    secret = secrets.token_urlsafe(32)
    invitation.secret_hash = sha256(secret.encode()).hexdigest()
    invitation.expires_at = _now() + timedelta(days=7)
    user_db.session.commit()
    token = create_access_token(
        identity=invitation.id,
        additional_claims={
            CLAIM_LIMITED_SCOPE: SCOPE_ACCEPT_INVITATION,
            "invitation_secret": secret,
        },
        expires_delta=timedelta(days=7),
    )
    run_task(
        send_email_invitation,
        email=invitation.email,
        token=token,
        tree_id=invitation.tree,
    )


class InvitationArgs(Schema):
    """Only owners choose the destination email, role, and tree."""

    email = fields.Email(required=True)
    role = fields.Integer(
        required=True, strict=True, validate=validate.Range(min=0, max=ROLE_ADMIN)
    )
    tree = fields.String()


class UserInvitationsResource(ProtectedResource):
    """List pending invitations and invite a user."""

    def get(self):
        query = user_db.session.query(UserInvitation)
        if not has_permissions([PERM_VIEW_OTHER_TREE_USER]):
            require_permissions([PERM_VIEW_OTHER_USER])
            query = query.filter_by(tree=get_tree_from_jwt())
        return jsonify(
            [
                _details(invitation)
                for invitation in query.order_by(UserInvitation.email)
            ]
        )

    @api_blueprint.arguments(InvitationArgs, location="json")
    def post(self, args):
        tree = args.get("tree") or get_tree_from_jwt()
        require_permissions(
            [PERM_ADD_USER if tree == get_tree_from_jwt() else PERM_ADD_OTHER_TREE_USER]
        )
        if args["role"] == ROLE_ADMIN:
            require_permissions([PERM_MAKE_ADMIN])
        if tree and not tree_exists(tree):
            abort_with_message(422, "Tree does not exist")
        if (
            not tree
            and current_app.config["TREE"] == TREE_MULTI
            and args["role"] < ROLE_ADMIN
        ):
            abort_with_message(422, "Tree is required")
        email = args["email"].strip().lower()
        if _existing_email(email, tree):
            abort_with_message(
                409, "A user with this email already exists in this tree"
            )
        if (
            user_db.session.query(UserInvitation)
            .filter_by(email=email, tree=tree)
            .first()
        ):
            abort_with_message(
                409, "An invitation already exists; resend it from user management"
            )
        invitation = UserInvitation(
            id=str(uuid.uuid4()),
            email=email,
            role=args["role"],
            tree=tree,
        )
        user_db.session.add(invitation)
        try:
            _send_invitation(invitation)
        except IntegrityError:
            user_db.session.rollback()
            if (
                user_db.session.query(UserInvitation)
                .filter_by(email=email, tree=tree)
                .first()
            ):
                abort_with_message(409, "An invitation already exists for this email")
            raise
        return jsonify(_details(invitation)), 201


class UserInvitationResource(ProtectedResource):
    """Resend or revoke a pending invitation."""

    def _get_invitation(self, invitation_id, permission, other_tree_permission):
        invitation = user_db.session.get(UserInvitation, invitation_id)
        if invitation is None:
            abort_with_message(404, "Invitation does not exist")
        require_permissions(
            [
                (
                    permission
                    if invitation.tree == get_tree_from_jwt()
                    else other_tree_permission
                )
            ]
        )
        return invitation

    def post(self, invitation_id):
        invitation = self._get_invitation(
            invitation_id, PERM_ADD_USER, PERM_ADD_OTHER_TREE_USER
        )
        if invitation.role == ROLE_ADMIN:
            require_permissions([PERM_MAKE_ADMIN])
        _send_invitation(invitation)
        return jsonify(_details(invitation)), 200

    def delete(self, invitation_id):
        invitation = self._get_invitation(
            invitation_id, PERM_DEL_USER, PERM_DEL_OTHER_TREE_USER
        )
        user_db.session.delete(invitation)
        user_db.session.commit()
        return "", 200


class AcceptInvitationArgs(Schema):
    """Recipients choose their own names and password, never email or role."""

    name = fields.String(required=True, validate=validate.Length(min=1, max=255))
    full_name = fields.String(required=True, validate=validate.Length(min=1, max=255))
    password = fields.String(required=True, validate=validate.Length(min=1))


class UserAcceptInvitationResource(LimitedScopeProtectedResource):
    """Redeem a scoped, expiring, single-use invitation."""

    def _get_invitation(self):
        claims = get_jwt()
        if claims[CLAIM_LIMITED_SCOPE] != SCOPE_ACCEPT_INVITATION:
            abort_with_message(403, "Wrong token")
        invitation = user_db.session.get(UserInvitation, get_jwt_identity())
        secret_hash = sha256(claims["invitation_secret"].encode()).hexdigest()
        if invitation is None or not secrets.compare_digest(
            invitation.secret_hash, secret_hash
        ):
            abort_with_message(
                409, "This invitation was already used, revoked, or replaced"
            )
        if invitation.expires_at <= _now():
            abort_with_message(
                410, "This invitation has expired; ask the owner to resend it"
            )
        if invitation.tree and not tree_exists(invitation.tree):
            abort_with_message(422, "Tree does not exist")
        return invitation

    def get(self):
        invitation = self._get_invitation()
        return (
            render_template(
                "accept_invitation.html",
                email=invitation.email,
                login_url=get_config("BASE_URL").rstrip("/") + "/",
            ),
            200,
            {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"},
        )

    @limiter.limit("5/minute")
    @api_blueprint.arguments(AcceptInvitationArgs, location="json")
    def post(self, args):
        invitation = self._get_invitation()
        name = args["name"].strip()
        full_name = args["full_name"].strip()
        if (
            not name
            or name in ("-", "_")
            or any(ord(c) < 32 or c in "/\\" for c in name)
        ):
            abort_with_message(422, "Please choose a valid username")
        if not full_name:
            abort_with_message(422, "Full name cannot be empty")
        if _existing_email(invitation.email, invitation.tree):
            abort_with_message(
                409, "A user with this email already exists in this tree"
            )
        user = User(
            id=uuid.uuid4(),
            name=name,
            fullname=full_name,
            email=invitation.email,
            role=invitation.role,
            tree=invitation.tree,
            pwhash=hash_password(args["password"]),
        )
        # Consume the current secret and create the account in one transaction.
        consumed = user_db.session.execute(
            sa.delete(UserInvitation).where(
                UserInvitation.id == invitation.id,
                UserInvitation.secret_hash == invitation.secret_hash,
                UserInvitation.expires_at > _now(),
            )
        )
        if consumed.rowcount != 1:
            user_db.session.rollback()
            abort_with_message(
                409, "This invitation was already used, revoked, or replaced"
            )
        user_db.session.add(user)
        try:
            user_db.session.commit()
        except IntegrityError:
            user_db.session.rollback()
            if user_db.session.query(User).filter_by(name=name).first():
                abort_with_message(
                    409, "This username is already taken; please choose another"
                )
            raise
        return "", 201
