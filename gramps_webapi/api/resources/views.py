"""SQL-scoped compound responses for expensive frontend modules."""

from __future__ import annotations

import json
from collections import deque
from datetime import date
from typing import Any

from flask import Response
from gramps.gen.errors import HandleError
from gramps.gen.lib.json_utils import object_to_dict
from gramps.gen.proxy.proxybase import ProxyDbBase
from marshmallow import Schema, validate
from webargs import fields

from ...auth.const import PERM_VIEW_PRIVATE
from ..auth import has_permissions
from ..blueprint import api_blueprint
from ..cache import request_cache_decorator
from ..util import abort_with_message, get_db_handle, get_locale_for_language
from . import ProtectedResource
from .emit import GrampsJSONEncoder
from .object_query import _resolve_dialect, _resolve_treeid
from .people import PersonResourceHelper
from .relationship_scope import (
    RelationshipScope,
    compile_connection_path_query,
    compile_relationship_scope,
)
from ..relation_path import _step_relationship_type
from .schemas import EventSchema, FamilySchema, PersonSchema, RelationshipPathSchema
from .util import get_event_profile_for_object


class RelationshipGraphArgs(Schema):
    """Relationship graph query arguments."""

    degree = fields.Int(load_default=3, validate=validate.Range(min=0, max=12))
    direction = fields.Str(
        load_default="any",
        validate=validate.OneOf(["any", "ancestors", "descendants"]),
    )
    locale = fields.Str(load_default=None, validate=validate.Length(min=1, max=5))


class AnniversaryArgs(Schema):
    """Anniversary view query arguments."""

    month = fields.Int(
        load_default=lambda: date.today().month,
        validate=validate.Range(min=1, max=12),
    )
    day = fields.Int(
        load_default=lambda: date.today().day,
        validate=validate.Range(min=1, max=31),
    )
    degree = fields.Int(load_default=4, validate=validate.Range(min=0, max=12))
    limit = fields.Int(load_default=10, validate=validate.Range(min=1, max=100))
    locale = fields.Str(load_default=None, validate=validate.Length(min=1, max=5))


class MapScopeArgs(RelationshipGraphArgs):
    """Map scopes may intentionally span an entire ancestry."""

    degree = fields.Int(load_default=50, validate=validate.Range(min=0, max=100))


class ConnectionGraphArgs(Schema):
    """Connection graph query arguments."""

    locale = fields.Str(load_default=None, validate=validate.Length(min=1, max=5))


class RelationshipGraphResponse(Schema):
    people = fields.List(fields.Nested(PersonSchema), required=True)


class AnniversariesResponse(Schema):
    events = fields.List(fields.Nested(EventSchema), required=True)


class MapScopeResponse(Schema):
    people = fields.List(fields.Nested(PersonSchema), required=True)
    families = fields.List(fields.Nested(FamilySchema), required=True)
    events = fields.List(fields.Nested(EventSchema), required=True)


class HomePersonResponse(Schema):
    person = fields.Nested(PersonSchema, required=True, allow_none=True)


class ConnectionGraphResponse(Schema):
    path = fields.Nested(RelationshipPathSchema, required=True)
    people = fields.List(fields.Nested(PersonSchema), required=True)
    families = fields.List(fields.Nested(FamilySchema), required=True)


def _base_db(db: Any) -> Any:
    return db.basedb if isinstance(db, ProxyDbBase) else db


def _scope_sql(db: Any, scope: RelationshipScope) -> tuple[Any, str, list[Any]]:
    basedb = _base_db(db)
    dialect = _resolve_dialect(basedb)
    treeid = _resolve_treeid(basedb)
    sql, params = compile_relationship_scope(
        scope,
        dialect=dialect,
        treeid=treeid,
        include_private=has_permissions({PERM_VIEW_PRIVATE}),
    )
    return basedb, sql, params


def _scope_handles(db: Any, scope: RelationshipScope, relation: str) -> list[str]:
    basedb, cte, params = _scope_sql(db, scope)
    basedb.dbapi.execute(
        f"{cte}\nSELECT handle FROM relationship_scope_{relation}", params
    )
    return [row[0] for row in basedb.dbapi.fetchall()]


def _type_projection(value: Any) -> Any:
    """Keep only the enum fields used by map relationship helpers."""
    if not isinstance(value, dict):
        return value
    return {key: value.get(key) for key in ("value", "string")}


def _map_projection(object_type: str, item: dict) -> dict:
    """Project raw Gramps JSON to the fields needed by map trajectories."""
    if object_type == "person":
        return {
            "handle": item.get("handle"),
            "event_ref_list": [
                {"ref": ref.get("ref")} for ref in item.get("event_ref_list", [])
            ],
            "birth_ref_index": item.get("birth_ref_index"),
            "family_list": item.get("family_list", []),
        }
    if object_type == "family":
        return {
            "handle": item.get("handle"),
            "event_ref_list": [
                {"ref": ref.get("ref")} for ref in item.get("event_ref_list", [])
            ],
            "father_handle": item.get("father_handle"),
            "mother_handle": item.get("mother_handle"),
            "child_ref_list": [
                {
                    "ref": ref.get("ref"),
                    "frel": _type_projection(ref.get("frel")),
                    "mrel": _type_projection(ref.get("mrel")),
                }
                for ref in item.get("child_ref_list", [])
            ],
        }
    date_value = item.get("date") or {}
    return {
        "handle": item.get("handle"),
        "date": {
            key: date_value.get(key)
            for key in ("dateval", "sortval", "calendar", "modifier", "quality")
        },
        "place": item.get("place"),
        "type": _type_projection(item.get("type")),
    }


class RelationshipGraphViewResource(
    ProtectedResource, PersonResourceHelper, GrampsJSONEncoder
):
    """People and family extensions for one bounded relationship graph."""

    @api_blueprint.response(200, RelationshipGraphResponse())
    @api_blueprint.arguments(RelationshipGraphArgs, location="query")
    @request_cache_decorator
    def get(self, args: dict, person: str) -> Response:
        """Return one render-ready relationship graph response."""
        db = get_db_handle()
        handles = _scope_handles(
            db,
            RelationshipScope(
                person=person,
                max_degree=args["degree"],
                direction=args["direction"],
            ),
            "persons",
        )
        if not handles:
            abort_with_message(404, f"Person {person} not found")
        locale = get_locale_for_language(args["locale"], default=True)
        people = []
        extension_args = {
            "profile": ["self"],
            "extend": ["event_ref_list", "primary_parent_family", "family_list"],
        }
        for handle in handles:
            item = db.get_person_from_handle(handle)
            if item is not None:
                people.append(self.object_extend(item, extension_args, locale=locale))
        return self.response(200, {"people": people})


class HomePersonViewResource(
    ProtectedResource, PersonResourceHelper, GrampsJSONEncoder
):
    """Indexed home-person lookup with its display profile."""

    @api_blueprint.response(200, HomePersonResponse())
    @request_cache_decorator
    def get(self, person: str) -> Response:
        """Return a home person by handle or Gramps ID without a table scan."""
        db = get_db_handle()
        try:
            item = db.get_person_from_gramps_id(person)
        except HandleError:
            item = None
        if item is None:
            try:
                item = db.get_person_from_handle(person)
            except HandleError:
                item = None
        if item is None:
            return self.response(200, {"person": None})
        item = self.object_extend(item, {"profile": ["self"], "extend": ["media_list"]})
        return self.response(200, {"person": item})


class ConnectionGraphViewResource(
    ProtectedResource, PersonResourceHelper, GrampsJSONEncoder
):
    """One SQL-backed shortest connection and its render-ready objects."""

    @api_blueprint.response(200, ConnectionGraphResponse())
    @api_blueprint.arguments(ConnectionGraphArgs, location="query")
    @request_cache_decorator
    def get(self, args: dict, source: str, target: str) -> Response:
        """Return a shortest connection plus path and context objects."""
        db = get_db_handle()
        basedb = _base_db(db)
        sql, params = compile_connection_path_query(
            source,
            target,
            dialect=_resolve_dialect(basedb),
            treeid=_resolve_treeid(basedb),
            include_private=has_permissions({PERM_VIEW_PRIVATE}),
        )
        basedb.dbapi.execute(sql, params)
        rows = basedb.dbapi.fetchall()
        source_row = next((row for row in rows if row[0] == "source"), None)
        target_row = next((row for row in rows if row[0] == "target"), None)
        if source_row is None:
            abort_with_message(404, f"Person {source} not found")
        if target_row is None:
            abort_with_message(404, f"Person {target} not found")

        source_handle = source_row[1]
        target_handle = target_row[1]
        target_distance = target_row[2]
        steps = []
        if target_distance >= 0:
            neighbours: dict[str, list[tuple]] = {}
            for row in rows:
                if row[0] == "edge":
                    neighbours.setdefault(row[3], []).append(row)
            previous: dict[str, tuple] = {}
            visited = {source_handle}
            pending = deque([source_handle])
            while pending and target_handle not in visited:
                current = pending.popleft()
                for edge in neighbours.get(current, []):
                    destination = edge[1]
                    if destination in visited:
                        continue
                    visited.add(destination)
                    previous[destination] = edge
                    pending.append(destination)
                    if destination == target_handle:
                        break
            current = target_handle
            while current != source_handle:
                edge = previous[current]
                steps.append(
                    {
                        "from_handle": edge[3],
                        "to_handle": edge[1],
                        "family_handle": edge[4],
                        "relation": edge[5],
                    }
                )
                current = edge[3]
            steps.reverse()

        family_handles = list(dict.fromkeys(step["family_handle"] for step in steps))
        families = []
        families_by_handle = {}
        for handle in family_handles:
            try:
                family = db.get_family_from_handle(handle)
            except HandleError:
                continue
            if family is not None:
                families.append(family)
                families_by_handle[handle] = family

        for step in steps:
            family = families_by_handle[step["family_handle"]]
            step["relationship_type"] = _step_relationship_type(
                family,
                step["from_handle"],
                step["to_handle"],
                step["relation"],
            )

        path_handles = [source_handle] + [step["to_handle"] for step in steps]
        person_handles = list(path_handles)
        if target_handle not in person_handles:
            person_handles.append(target_handle)
        path_handle_set = set(path_handles)
        for step in steps:
            if step["relation"] not in {"child", "parent", "sibling"}:
                continue
            family = families_by_handle[step["family_handle"]]
            for handle in (family.get_father_handle(), family.get_mother_handle()):
                if (
                    handle
                    and handle not in path_handle_set
                    and handle not in person_handles
                ):
                    person_handles.append(handle)

        locale = get_locale_for_language(args["locale"], default=True)
        people = []
        for handle in person_handles:
            try:
                person = db.get_person_from_handle(handle)
            except HandleError:
                continue
            if person is not None:
                people.append(
                    self.object_extend(person, {"profile": ["self"]}, locale=locale)
                )

        path = {
            "connected": target_distance >= 0,
            "person_handles": path_handles if target_distance >= 0 else [],
            "family_handles": [step["family_handle"] for step in steps],
            "steps": steps,
        }
        return self.response(
            200, {"path": path, "people": people, "families": families}
        )


class AnniversariesViewResource(ProtectedResource, GrampsJSONEncoder):
    """Anniversaries selected in SQL from the shared relationship scope."""

    @api_blueprint.response(200, AnniversariesResponse())
    @api_blueprint.arguments(AnniversaryArgs, location="query")
    @request_cache_decorator
    def get(self, args: dict, person: str) -> Response:
        """Return related events occurring on one month/day."""
        db = get_db_handle()
        basedb, cte, params = _scope_sql(
            db, RelationshipScope(person=person, max_degree=args["degree"])
        )
        dialect = _resolve_dialect(basedb)
        treeid = _resolve_treeid(basedb)
        tree_clause = ""
        query_params = list(params)
        if dialect.value == "sqlite":
            sort_sql = "json_extract(event.json_data, '$.date.sortval')"
            day_sql = f"CAST(strftime('%d', {sort_sql}) AS integer)"
            month_sql = f"CAST(strftime('%m', {sort_sql}) AS integer)"
        else:
            sort_sql = "(event.json_data::jsonb #>> '{date,sortval}')::integer"
            date_sql = f"to_date(({sort_sql})::text, 'J')"
            day_sql = f"EXTRACT(DAY FROM {date_sql})"
            month_sql = f"EXTRACT(MONTH FROM {date_sql})"
        query_params.extend([args["day"], args["month"]])
        if treeid is not None:
            tree_clause = " AND event.treeid = ?"
            query_params.append(treeid)
        query_params.append(args["limit"])
        sql = f"""
{cte}
SELECT event.handle
FROM event
JOIN relationship_scope_events AS scoped ON scoped.handle = event.handle
WHERE {sort_sql} > 0 AND {day_sql} = ? AND {month_sql} = ?{tree_clause}
ORDER BY {sort_sql} DESC, event.handle
LIMIT ?
"""
        basedb.dbapi.execute(sql, query_params)
        handles = [row[0] for row in basedb.dbapi.fetchall()]
        locale = get_locale_for_language(args["locale"], default=True)
        events = []
        for handle in handles:
            event = db.get_event_from_handle(handle)
            if event is None:
                continue
            event.profile = get_event_profile_for_object(
                db, event, args=["participants"], locale=locale
            )
            events.append(event)
        return self.response(200, {"events": events})


class MapScopeViewResource(ProtectedResource, GrampsJSONEncoder):
    """Person, family, and event facts for one map relationship scope."""

    @api_blueprint.response(200, MapScopeResponse())
    @api_blueprint.arguments(MapScopeArgs, location="query")
    @request_cache_decorator
    def get(self, args: dict, person: str) -> Response:
        """Return the three shared relations used to derive map trajectories."""
        db = get_db_handle()
        basedb, cte, params = _scope_sql(
            db,
            RelationshipScope(
                person=person,
                max_degree=args["degree"],
                direction=args["direction"],
            ),
        )
        basedb.dbapi.execute(
            f"""{cte}
SELECT 'person', person.handle, person.json_data
FROM person
JOIN relationship_scope_persons AS scoped ON scoped.handle = person.handle
UNION ALL
SELECT 'family', family.handle, family.json_data
FROM family
JOIN relationship_scope_families AS scoped ON scoped.handle = family.handle
UNION ALL
SELECT 'event', event.handle, event.json_data
FROM event
JOIN relationship_scope_events AS scoped ON scoped.handle = event.handle
""",
            params,
        )
        objects = {"people": [], "families": [], "events": []}
        response_keys = {"person": "people", "family": "families", "event": "events"}
        getters = {
            "person": db.get_person_from_handle,
            "family": db.get_family_from_handle,
            "event": db.get_event_from_handle,
        }
        for object_type, handle, raw in basedb.dbapi.fetchall():
            if isinstance(db, ProxyDbBase):
                try:
                    obj = getters[object_type](handle)
                except HandleError:
                    continue
                if obj is None:
                    continue
                item = object_to_dict(obj)
            else:
                item = json.loads(raw) if isinstance(raw, str) else raw
            objects[response_keys[object_type]].append(
                _map_projection(object_type, item)
            )
        if not objects["people"]:
            abort_with_message(404, f"Person {person} not found")
        return self.response(200, objects)
