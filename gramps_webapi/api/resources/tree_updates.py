"""Authenticated server-sent events for the current family tree."""

import time

from flask import Response
from flask_jwt_extended import get_jwt, get_jwt_identity

from ...auth.const import PERM_VIEW_PRIVATE
from ..auth import has_permissions
from ..tree_updates import event_broker, tree_event_stream, tree_updates_url
from ..util import abort_with_message, get_tree_from_jwt_or_fail
from . import ProtectedResource


class TreeUpdatesResource(ProtectedResource):
    """Push notifications scoped to the authenticated user's tree."""

    def get(self) -> Response:
        tree = get_tree_from_jwt_or_fail()
        url = tree_updates_url()
        if not url:
            abort_with_message(503, "Tree update streaming requires a Redis broker")
        # Reauthenticate periodically, and never keep a stream beyond JWT expiry.
        expires = min(get_jwt()["exp"], time.time() + 15 * 60)
        stream = tree_event_stream(
            event_broker(url),
            tree,
            get_jwt_identity(),
            expires,
            include_private=has_permissions({PERM_VIEW_PRIVATE}),
        )
        return Response(
            stream,
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "private, no-store, no-transform",
                "X-Accel-Buffering": "no",
            },
        )
