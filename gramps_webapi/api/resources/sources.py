#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2020      David Straub
# Copyright (C) 2020      Christopher Horn
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

"""Source API resource."""

from flask import abort, jsonify
from gramps.gen.errors import HandleError

from ...auth import User
from ..util import get_tree_from_jwt_or_fail
from . import ProtectedResource
from .base import (
    GrampsObjectProtectedResource,
    GrampsObjectResourceHelper,
    GrampsObjectsProtectedResource,
)


class SourceResourceHelper(GrampsObjectResourceHelper):
    """Source resource helper."""

    gramps_class_name = "Source"


class SourceResource(GrampsObjectProtectedResource, SourceResourceHelper):
    """Source resource."""


class SourcesResource(GrampsObjectsProtectedResource, SourceResourceHelper):
    """Sources resource."""


class SourceAuthorResource(ProtectedResource, SourceResourceHelper):
    """Public author identity for a source visible to the current reader."""

    def get(self, handle):
        try:
            source = self.get_object_from_handle(handle)
        except HandleError:
            abort(404)
        if source is None:
            abort(404)
        result = {"name": source.get_author(), "username": None, "person_id": None}
        user_id = next(
            (
                a.get_value()
                for a in source.get_attribute_list()
                if str(a.get_type()) == "Blog author"
            ),
            None,
        )
        if not user_id:
            return jsonify(result)
        user = User.query.filter_by(
            id=user_id, tree=get_tree_from_jwt_or_fail()
        ).first()
        if user is None:
            return jsonify(result)
        result.update(name=user.fullname or user.name, username=user.name)
        person_id = (user.settings or {}).get("homePerson")
        if person_id:
            try:
                person = self.db_handle.get_person_from_gramps_id(person_id)
            except HandleError:
                person = None
            if person is not None:
                result["person_id"] = person.gramps_id
        return jsonify(result)
