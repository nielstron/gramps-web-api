#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2020      Christopher Horn
# Copyright (C) 2025      David Straub
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

"""Relation API Resource."""

from collections import defaultdict, deque
from typing import Dict

from flask import Response
from gramps.gen.errors import HandleError
from gramps.gen.lib import ChildRef, Family, Person
from gramps.gen.proxy.proxybase import ProxyDbBase
from gramps.gen.relationship import get_relationship_calculator
from marshmallow import Schema
from webargs import fields, validate

from gramps_webapi.api.people_families_cache import CachePeopleFamiliesProxy
from gramps_webapi.api.relation_path import find_connection_path

from ...types import Handle
from ..blueprint import api_blueprint
from ..cache import request_cache_decorator
from ..util import abort_with_message, get_db_handle, get_locale_for_language
from . import ProtectedResource
from .emit import GrampsJSONEncoder
from .object_query import _resolve_dialect, _resolve_treeid
from .schemas import (
    RelationshipItemSchema,
    RelationshipPathSchema,
    RelationshipSchema,
)
from .util import get_one_relationship


class RelationQueryArgs(Schema):
    """Query arguments for relation endpoints."""

    depth = fields.Integer(
        load_default=15,
        validate=validate.Range(min=2),
        metadata={
            "description": "Maximum number of generations to search for a common ancestor (default 15)."
        },
    )
    locale = fields.Str(
        load_default=None,
        validate=validate.Length(min=1, max=5),
        metadata={
            "description": "Language code of the locale to use where applicable. Must be a valid code from the available translations."
        },
    )


def _relationship_subset_db(
    db_handle, person1: Person, person2: Person, depth: int
) -> CachePeopleFamiliesProxy:
    """Load the compact parent-edge table with one SQL statement.

    Gramps' relationship calculator retains its exact localized and non-birth
    relationship semantics, but traverses this bounded in-memory subset rather
    than issuing one lookup per ancestor or preloading the complete tree.
    """
    dialect = _resolve_dialect(db_handle)
    treeid = _resolve_treeid(db_handle)
    params = []
    tree_person = ""
    tree_family = ""
    if treeid is not None:
        tree_person = "WHERE person.treeid = ?"
        tree_family = " AND family.treeid = person.treeid"
        params.append(treeid)
    if dialect.value == "sqlite":
        parent_families = (
            "JOIN json_each(person.json_data, '$.parent_family_list') "
            "AS parent_family"
        )
        children = "JOIN json_each(family.json_data, '$.child_ref_list') AS child"
        family_handle = "parent_family.value"
        family_index = "CAST(parent_family.key AS integer)"
        child_handle = "json_extract(child.value, '$.ref')"
        father_relation = "json_extract(child.value, '$.frel.value')"
        mother_relation = "json_extract(child.value, '$.mrel.value')"
    else:
        parent_families = """
CROSS JOIN LATERAL jsonb_array_elements_text(
    COALESCE(person.json_data::jsonb -> 'parent_family_list', '[]'::jsonb)
) WITH ORDINALITY AS parent_family(value, ordinal)
""".strip()
        children = """
JOIN LATERAL jsonb_array_elements(
    COALESCE(family.json_data::jsonb -> 'child_ref_list', '[]'::jsonb)
) AS child(value)
""".strip()
        family_handle = "parent_family.value"
        family_index = "parent_family.ordinal - 1"
        child_handle = "child.value ->> 'ref'"
        father_relation = "child.value #>> '{frel,value}'"
        mother_relation = "child.value #>> '{mrel,value}'"
    db_handle.dbapi.execute(
        f"""
SELECT person.handle, family.handle, {family_index},
       family.father_handle, family.mother_handle,
       CAST({father_relation} AS integer), CAST({mother_relation} AS integer)
FROM person
{parent_families}
JOIN family ON family.handle = {family_handle}{tree_family}
{children} ON {child_handle} = person.handle
{tree_person}
ORDER BY person.handle, {family_index}, family.handle
""",
        params,
    )
    edges_by_child = defaultdict(list)
    edges_by_family = defaultdict(list)
    for row in db_handle.dbapi.fetchall():
        edges_by_child[row[0]].append(row)
        edges_by_family[row[1]].append(row)

    roots = (person1.handle, person2.handle)
    reachable = set(roots)
    family_handles = set()
    queue = deque((handle, 0) for handle in roots)
    while queue:
        handle, distance = queue.popleft()
        if distance >= depth:
            continue
        for edge in edges_by_child.get(handle, ()):
            family_handles.add(edge[1])
            for parent_handle in edge[3:5]:
                if parent_handle and parent_handle not in reachable:
                    reachable.add(parent_handle)
                    queue.append((parent_handle, distance + 1))

    people = []
    for handle in reachable:
        person = Person()
        person.set_handle(handle)
        person.set_parent_family_handle_list(
            [edge[1] for edge in edges_by_child.get(handle, ())]
        )
        people.append(person)

    families = []
    for handle in family_handles:
        rows = edges_by_family[handle]
        family = Family()
        family.set_handle(handle)
        family.set_father_handle(rows[0][3] or None)
        family.set_mother_handle(rows[0][4] or None)
        for child_handle, _, _, _, _, father_relation, mother_relation in rows:
            child_ref = ChildRef()
            child_ref.set_reference_handle(child_handle)
            child_ref.set_father_relation(father_relation)
            child_ref.set_mother_relation(mother_relation)
            family.add_child_ref(child_ref)
        families.append(family)

    subset = CachePeopleFamiliesProxy(db_handle)
    subset.prime_people(people)
    subset.prime_families(families)
    # Preserve the complete root records for gender, partner families, and
    # names used in loop diagnostics.
    subset.prime_people((person1, person2))
    return subset


def _get_one_relationship_scoped(
    db_handle,
    handle1: Handle,
    handle2: Handle,
    depth: int,
    locale,
) -> tuple[str, int, int]:
    """Calculate a relation from a SQL-selected ancestor subset."""

    def calculate(limit: int) -> tuple[str, int, int]:
        person1 = db_handle.get_person_from_handle(handle1)
        person2 = db_handle.get_person_from_handle(handle2)
        subset = _relationship_subset_db(db_handle, person1, person2, limit)
        return get_one_relationship(
            db_handle=subset,
            person1=subset.get_person_from_handle(handle1),
            person2=subset.get_person_from_handle(handle2),
            depth=limit,
            locale=locale,
        )

    first_depth = min(depth, 5)
    result = calculate(first_depth)
    if depth <= 5 or result[0] or result[1] > -1 or handle1 == handle2:
        return result
    return calculate(depth)


class RelationResource(ProtectedResource, GrampsJSONEncoder):
    """Relation resource."""

    @api_blueprint.response(200, RelationshipSchema())
    @api_blueprint.arguments(RelationQueryArgs, location="query")
    @request_cache_decorator
    def get(self, args: Dict, handle1: Handle, handle2: Handle) -> Response:
        """Get the most direct relationship between two people."""
        db = get_db_handle()
        db_handle = CachePeopleFamiliesProxy(db)
        try:
            person1 = db_handle.get_person_from_handle(handle1)
        except HandleError:
            abort_with_message(404, f"Person {handle1} not found")
        try:
            person2 = db_handle.get_person_from_handle(handle2)
        except HandleError:
            abort_with_message(404, f"Person {handle2} not found")

        locale = get_locale_for_language(args["locale"], default=True)
        if isinstance(db, ProxyDbBase):
            data = get_one_relationship(
                db_handle=db_handle,
                person1=person1,
                person2=person2,
                depth=args["depth"],
                locale=locale,
            )
        else:
            data = _get_one_relationship_scoped(
                db, handle1, handle2, args["depth"], locale
            )
        return self.response(
            200,
            {
                "relationship_string": data[0],
                "distance_common_origin": data[1],
                "distance_common_other": data[2],
            },
        )


class RelationsResource(ProtectedResource, GrampsJSONEncoder):
    """Relations resource."""

    @api_blueprint.response(200, RelationshipItemSchema(many=True))
    @api_blueprint.arguments(RelationQueryArgs, location="query")
    @request_cache_decorator
    def get(self, args: Dict, handle1: Handle, handle2: Handle) -> Response:
        """Get all possible relationships between two people."""
        db_handle = CachePeopleFamiliesProxy(get_db_handle())

        try:
            person1 = db_handle.get_person_from_handle(handle1)
        except HandleError:
            abort_with_message(404, f"Person {handle1} not found")

        try:
            person2 = db_handle.get_person_from_handle(handle2)
        except HandleError:
            abort_with_message(404, f"Person {handle2} not found")

        db_handle.cache_people()
        db_handle.cache_families()

        locale = get_locale_for_language(args["locale"], default=True)
        calc = get_relationship_calculator(reinit=True, clocale=locale)
        calc.set_depth(args["depth"])

        data = calc.get_all_relationships(db_handle, person1, person2)
        result = []
        index = 0
        while index < len(data[0]):
            result.append(
                {
                    "relationship_string": data[0][index],
                    "common_ancestors": data[1][index],
                }
            )
            index = index + 1
        if result == []:
            result = [{}]
        return self.response(200, result)


class RelationPathResource(ProtectedResource, GrampsJSONEncoder):
    """Shortest connection through parent, child, partner, and sibling links."""

    @api_blueprint.response(200, RelationshipPathSchema())
    @request_cache_decorator
    def get(self, handle1: Handle, handle2: Handle) -> Response:
        """Get a shortest path through the complete family graph."""
        db_handle = CachePeopleFamiliesProxy(get_db_handle())
        for handle in (handle1, handle2):
            try:
                db_handle.get_person_from_handle(handle)
            except HandleError:
                abort_with_message(404, f"Person {handle} not found")
        db_handle.cache_people()
        db_handle.cache_families()
        return self.response(200, find_connection_path(db_handle, handle1, handle2))
