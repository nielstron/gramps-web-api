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

"""Person API resource."""

from typing import Dict

from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.errors import HandleError
from gramps.gen.lib import Person
from gramps.gen.utils.grampslocale import GrampsLocale

from ..util import abort_with_message
from .base import (
    GrampsObjectProtectedResource,
    GrampsObjectResourceHelper,
    GrampsObjectsProtectedResource,
)
from .util import (
    get_extended_attributes,
    get_family_by_handle,
    get_person_profile_for_object,
)


class PersonResourceHelper(GrampsObjectResourceHelper):
    """Person resource helper."""

    gramps_class_name = "Person"

    def object_extend(
        self, obj: Person, args: Dict, locale: GrampsLocale = glocale
    ) -> Person:
        """Extend person attributes as needed."""
        db_handle = self.db_handle
        if "profile" in args:
            obj.profile = get_person_profile_for_object(
                db_handle,
                obj,
                args["profile"],
                locale=locale,
                name_format=args.get("name_format"),
                precision=args.get("precision", 3),
            )
            if args.get("relationship_to"):
                from .relations import get_one_relationship_for_people

                try:
                    anchor = db_handle.get_person_from_handle(args["relationship_to"])
                except HandleError:
                    anchor = None
                if anchor is None:
                    abort_with_message(
                        404,
                        f"Person {args['relationship_to']} not found",
                    )
                relation = get_one_relationship_for_people(
                    db_handle, anchor, obj, depth=100, locale=locale
                )
                obj.profile["relationship_to"] = {
                    "relationship_string": relation[0],
                    "distance_common_origin": relation[1],
                    "distance_common_other": relation[2],
                }
        if "extend" in args:
            obj.extended = get_extended_attributes(db_handle, obj, args)
            if "all" in args["extend"] or "family_list" in args["extend"]:
                obj.extended["families"] = [
                    get_family_by_handle(db_handle, handle)
                    for handle in obj.family_list
                ]
            if "all" in args["extend"] or "parent_family_list" in args["extend"]:
                obj.extended["parent_families"] = [
                    get_family_by_handle(db_handle, handle)
                    for handle in obj.parent_family_list
                ]
            if "all" in args["extend"] or "primary_parent_family" in args["extend"]:
                obj.extended["primary_parent_family"] = get_family_by_handle(
                    db_handle, obj.get_main_parents_family_handle()
                )
        return obj


class PersonResource(GrampsObjectProtectedResource, PersonResourceHelper):
    """Person resource."""


class PeopleResource(GrampsObjectsProtectedResource, PersonResourceHelper):
    """People resource."""
