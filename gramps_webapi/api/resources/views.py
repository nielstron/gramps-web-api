"""SQL-scoped compound responses for expensive frontend modules."""

from __future__ import annotations

import json
from collections import deque
from datetime import date
from typing import Any

from flask import Response
from gramps.gen.errors import HandleError
from gramps.gen.lib.json_utils import data_to_object, object_to_dict
from gramps.gen.proxy.proxybase import ProxyDbBase
from marshmallow import Schema, validate
from webargs import fields

from ...auth.const import PERM_VIEW_PRIVATE
from ..auth import has_permissions
from ..blueprint import api_blueprint
from ..cache import request_cache_decorator
from ..relation_path import _step_relationship_type
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
from .schemas import (
    EventSchema,
    FamilySchema,
    PersonSchema,
    RelationshipPathSchema,
    SearchResultSchema,
)
from .util import (
    display_date,
    get_event_profile_for_object,
    get_person_profile_for_object,
)


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


class RecentChangesArgs(Schema):
    """Arguments for the compact dashboard change feed."""

    since = fields.Float(load_default=0, validate=validate.Range(min=0))
    limit = fields.Int(load_default=8, validate=validate.Range(min=1, max=100))


class RelationshipGraphResponse(Schema):
    people = fields.List(fields.Nested(PersonSchema), required=True)
    families = fields.List(fields.Nested(FamilySchema), required=True)


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


def _json_fragment(value: Any, default: Any) -> Any:
    """Decode a JSON column fragment returned by SQLite or PostgreSQL."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _recent_related_objects(
    basedb: Any,
    table: str,
    handles: set[str],
    *,
    include_private: bool,
) -> dict[str, dict]:
    """Load the few records needed to label recent families and citations."""
    if not handles:
        return {}
    treeid = _resolve_treeid(basedb)
    placeholders = ", ".join("?" for _ in handles)
    params: list[Any] = list(handles)
    filters = [f"handle IN ({placeholders})"]
    if treeid is not None:
        filters.append("treeid = ?")
        params.append(treeid)
    if not include_private:
        filters.append("private = 0")
    basedb.dbapi.execute(
        f"SELECT handle, json_data FROM {table} WHERE {' AND '.join(filters)}",
        params,
    )
    return {handle: _json_fragment(raw, {}) for handle, raw in basedb.dbapi.fetchall()}


def _recent_person_profile(item: dict | None) -> dict:
    """Project a person name to the shape used in family result labels."""
    if not item:
        return {}
    name = _project_name(item.get("primary_name"))
    return {
        "gramps_id": item.get("gramps_id"),
        "name_given": name.get("first_name") or "",
        "name_surname": _surname(name),
        "name_suffix": name.get("suffix") or "",
        "name_title": name.get("title") or "",
    }


def _recent_projection(
    object_type: str,
    item: dict,
    people: dict[str, dict],
    sources: dict[str, dict],
) -> dict:
    """Return only the fields rendered by the dashboard's change feed."""
    result = {key: item.get(key) for key in ("handle", "gramps_id", "change")}
    if object_type == "person":
        result.update(
            {
                "gender": item.get("gender"),
                "primary_name": _project_name(item.get("primary_name")),
                "media_list": [
                    {"ref": ref.get("ref"), "rect": ref.get("rect") or []}
                    for ref in item.get("media_list", [])[:1]
                    if not ref.get("private") and ref.get("ref")
                ],
            }
        )
    elif object_type == "family":
        father = _recent_person_profile(people.get(item.get("father_handle")))
        mother = _recent_person_profile(people.get(item.get("mother_handle")))
        result["profile"] = {
            key: profile
            for key, profile in (("father", father), ("mother", mother))
            if profile
        }
    elif object_type == "event":
        result["type"] = item.get("type")
    elif object_type == "place":
        result.update({"name": item.get("name"), "title": item.get("title")})
    elif object_type == "source":
        result["title"] = item.get("title")
    elif object_type == "citation":
        source = sources.get(item.get("source_handle"), {})
        result["profile"] = {
            "page": item.get("page") or "",
            "source": {"title": source.get("title") or ""},
        }
    elif object_type == "repository":
        result.update({"name": item.get("name"), "type": item.get("type")})
    elif object_type == "media":
        result.update(
            {
                "desc": item.get("desc"),
                "mime": item.get("mime"),
                "checksum": item.get("checksum"),
            }
        )
    elif object_type == "note":
        result["type"] = item.get("type")
    elif object_type == "tag":
        result.update({"name": item.get("name"), "color": item.get("color")})
    return result


def get_recent_changes_view(db: Any, *, since: float, limit: int) -> list[dict]:
    """Select and project the newest primary objects without a search index."""
    basedb = _base_db(db)
    treeid = _resolve_treeid(basedb)
    include_private = has_permissions({PERM_VIEW_PRIVATE})
    object_types = (
        "person",
        "family",
        "event",
        "place",
        "citation",
        "source",
        "repository",
        "media",
        "note",
        "tag",
    )
    selects = []
    params: list[Any] = []
    for object_type in object_types:
        filters = ["change > ?"]
        params.append(since)
        if treeid is not None:
            filters.append("treeid = ?")
            params.append(treeid)
        if not include_private:
            filters.append("private = 0")
        selects.append(
            f"SELECT '{object_type}' AS object_type, handle, change "
            f"FROM {object_type} WHERE {' AND '.join(filters)}"
        )
    params.append(limit)
    object_rows = " UNION ALL ".join(
        f"SELECT recent.object_type, recent.handle, recent.change, "
        f"{object_type}.json_data "
        f"FROM recent JOIN {object_type} ON recent.object_type = '{object_type}' "
        f"AND {object_type}.handle = recent.handle"
        for object_type in object_types
    )
    basedb.dbapi.execute(
        f"""
WITH changed AS ({' UNION ALL '.join(selects)}),
recent AS (
  SELECT object_type, handle, change
  FROM changed
  ORDER BY change DESC, object_type, handle
  LIMIT ?
)
SELECT object_type, handle, change, json_data
FROM ({object_rows}) AS recent_objects
ORDER BY change DESC, object_type, handle
""",
        params,
    )
    rows = [
        (object_type, handle, change, _json_fragment(raw, {}))
        for object_type, handle, change, raw in basedb.dbapi.fetchall()
    ]
    family_people = {
        handle
        for object_type, _, _, item in rows
        if object_type == "family"
        for handle in (item.get("father_handle"), item.get("mother_handle"))
        if handle
    }
    citation_sources = {
        item.get("source_handle")
        for object_type, _, _, item in rows
        if object_type == "citation" and item.get("source_handle")
    }
    people = _recent_related_objects(
        basedb, "person", family_people, include_private=include_private
    )
    sources = _recent_related_objects(
        basedb, "source", citation_sources, include_private=include_private
    )
    return [
        {
            "handle": handle,
            "object_type": object_type,
            "object": _recent_projection(object_type, item, people, sources),
        }
        for object_type, handle, _, item in rows
    ]


def _type_name(value: Any, names: dict[int, str], default: str) -> str:
    """Return the stable XML name of a projected Gramps type."""
    if not isinstance(value, dict):
        return value or default
    custom = value.get("string")
    if custom:
        return custom
    return names.get(value.get("value"), default)


def _project_name(value: Any) -> dict:
    name = _json_fragment(value, {})
    if not name or name.get("private"):
        return {}
    name = {
        key: name.get(key)
        for key in ("first_name", "suffix", "title", "call", "surname_list")
    } | {
        "type": _type_name(
            name.get("type"),
            {
                -1: "Unknown",
                0: "Custom",
                1: "Also Known As",
                2: "Birth Name",
                3: "Married Name",
            },
            "Unknown",
        )
    }
    name["surname_list"] = [
        {key: surname.get(key) for key in ("surname", "prefix", "connector", "primary")}
        for surname in name.get("surname_list") or []
    ]
    return name


def _project_date(value: Any, locale: Any) -> dict:
    raw = _json_fragment(value, {})
    if not raw:
        return {}
    return {"date": display_date(data_to_object(raw), locale)}


def _surname(name: dict) -> str:
    return " ".join(
        part
        for surname in name.get("surname_list", [])
        for part in (
            surname.get("prefix"),
            surname.get("surname"),
            surname.get("connector"),
        )
        if part
    )


def _person_graph_projection(row: tuple, locale: Any) -> dict:
    """Build the compact person shape consumed by relationship cards."""
    (
        _,
        handle,
        gramps_id,
        gender,
        primary_raw,
        alternates_raw,
        media_raw,
        family_handles_raw,
        primary_parent_handle,
        birth_raw,
        death_raw,
        *_,
    ) = row
    primary = _project_name(primary_raw)
    alternates = [
        projected
        for value in _json_fragment(alternates_raw, [])
        if (projected := _project_name(value))
    ]
    media = _json_fragment(media_raw, {})
    media_list = []
    if media and not media.get("private") and media.get("ref"):
        media_list.append({"ref": media["ref"], "rect": media.get("rect") or []})
    first_name = primary.get("first_name") or ""
    surname = _surname(primary)
    title = primary.get("title") or ""
    display = " ".join(part for part in (title, first_name, surname) if part)
    return {
        "handle": handle,
        "gramps_id": gramps_id,
        "primary_name": primary,
        "alternate_names": alternates,
        "media_list": media_list,
        "profile": {
            "handle": handle,
            "gramps_id": gramps_id,
            "sex": {0: "F", 1: "M", 3: "X"}.get(gender, "U"),
            "birth": _project_date(birth_raw, locale),
            "death": _project_date(death_raw, locale),
            "name_given": first_name,
            "name_surname": surname,
            "name_display": display,
            "name_suffix": primary.get("suffix") or "",
            "name_title": title,
        },
        "_family_handles": _json_fragment(family_handles_raw, []),
        "_primary_parent_handle": primary_parent_handle,
    }


def _family_graph_projection(row: tuple) -> dict:
    """Build the compact family shape consumed by relationship edges."""
    handle, father, mother, type_raw, children_raw = row[11:]
    child_types = {
        0: "None",
        1: "Birth",
        2: "Adopted",
        3: "Stepchild",
        4: "Sponsored",
        5: "Foster",
        6: "Unknown",
        7: "Custom",
    }
    children = []
    for child in _json_fragment(children_raw, []):
        if child.get("private"):
            continue
        children.append(
            {
                "ref": child.get("ref"),
                "frel": _type_name(child.get("frel"), child_types, "Unknown"),
                "mrel": _type_name(child.get("mrel"), child_types, "Unknown"),
            }
        )
    return {
        "handle": handle,
        "father_handle": father or "",
        "mother_handle": mother or "",
        "type": _type_name(
            _json_fragment(type_raw, {}),
            {
                0: "Married",
                1: "Unmarried",
                2: "Civil Union",
                3: "Unknown",
                4: "Custom",
            },
            "Unknown",
        ),
        "child_ref_list": children,
    }


def _person_event_ref_sql(dialect: Any, kind: str, treeid: Any) -> str:
    """Select a preferred birth/death ref, including Gramps' fallbacks."""
    index = f"person.{kind}_ref_index"
    fallback_types = {
        "birth": (45, 15, 22),
        "death": (45, 19, 24, 20, 39),
    }[kind]
    type_list = ", ".join(str(value) for value in fallback_types)
    tree_clause = (
        " AND fallback_event.treeid = person.treeid" if treeid is not None else ""
    )
    if dialect.value == "sqlite":
        indexed = (
            f"CASE WHEN {index} >= 0 THEN json_extract(person.json_data, "
            f"'$.event_ref_list[' || {index} || '].ref') END"
        )
        fallback = f"""
SELECT json_extract(ref.value, '$.ref')
FROM json_each(person.json_data, '$.event_ref_list') AS ref
JOIN event AS fallback_event
  ON fallback_event.handle = json_extract(ref.value, '$.ref'){tree_clause}
WHERE json_extract(ref.value, '$.role.value') = 1
  AND json_extract(fallback_event.json_data, '$.type.value') IN ({type_list})
ORDER BY CAST(ref.key AS integer)
LIMIT 1
"""
    else:
        indexed = (
            f"CASE WHEN {index} >= 0 THEN person.json_data::jsonb "
            f"-> 'event_ref_list' -> {index} ->> 'ref' END"
        )
        fallback = f"""
SELECT ref.value ->> 'ref'
FROM jsonb_array_elements(
       COALESCE(person.json_data::jsonb -> 'event_ref_list', '[]'::jsonb)
     ) WITH ORDINALITY AS ref(value, ordinal)
JOIN event AS fallback_event
  ON fallback_event.handle = ref.value ->> 'ref'{tree_clause}
WHERE (ref.value #>> '{{role,value}}')::integer = 1
  AND (fallback_event.json_data::jsonb #>> '{{type,value}}')::integer
      IN ({type_list})
ORDER BY ref.ordinal
LIMIT 1
"""
    return f"COALESCE({indexed}, ({fallback.strip()}))"


def _relationship_graph_rows(
    basedb: Any, cte: str, params: list[Any], dialect: Any
) -> list[tuple]:
    """Fetch all card and edge fields with one projected SQL statement."""
    if dialect.value == "sqlite":

        def json_value(alias: str, path: str) -> str:
            return f"json_extract({alias}.json_data, '{path}')"

    else:

        def json_value(alias: str, path: str) -> str:
            parts = (
                path.removeprefix("$.")
                .replace(".", ",")
                .replace("[", ",")
                .replace("]", "")
            )
            return f"{alias}.json_data::jsonb #> '{{{parts}}}'"

    treeid = _resolve_treeid(basedb)
    birth_ref = _person_event_ref_sql(dialect, "birth", treeid)
    death_ref = _person_event_ref_sql(dialect, "death", treeid)
    tree_join = ""
    if treeid is not None:
        tree_join = " AND {event}.treeid = person.treeid"
    sql = f"""
{cte}
SELECT 'person', person.handle, person.gramps_id, person.gender,
       {json_value("person", "$.primary_name")},
       {json_value("person", "$.alternate_names")},
       {json_value("person", "$.media_list[0]")},
       {json_value("person", "$.family_list")},
       {json_value("person", "$.parent_family_list[0]")},
       {json_value("birth_event", "$.date")},
       {json_value("death_event", "$.date")},
       NULL, NULL, NULL, NULL, NULL
FROM person
JOIN relationship_scope_persons AS scoped ON scoped.handle = person.handle
LEFT JOIN event AS birth_event ON birth_event.handle = {birth_ref}{tree_join.format(event="birth_event")}
LEFT JOIN event AS death_event ON death_event.handle = {death_ref}{tree_join.format(event="death_event")}
UNION ALL
SELECT 'family', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       family.handle, family.father_handle, family.mother_handle,
       {json_value("family", "$.type")},
       {json_value("family", "$.child_ref_list")}
FROM family
JOIN relationship_scope_families AS scoped ON scoped.handle = family.handle
"""
    basedb.dbapi.execute(sql, params)
    return basedb.dbapi.fetchall()


def _home_person_projection(db: Any, person: str, locale: Any) -> dict | None:
    """Fetch one home-person card directly from indexed columns and JSON paths."""
    basedb = _base_db(db)
    dialect = _resolve_dialect(basedb)
    treeid = _resolve_treeid(basedb)
    if dialect.value == "sqlite":

        def json_value(alias: str, path: str) -> str:
            return f"json_extract({alias}.json_data, '{path}')"

    else:

        def json_value(alias: str, path: str) -> str:
            parts = (
                path.removeprefix("$.")
                .replace(".", ",")
                .replace("[", ",")
                .replace("]", "")
            )
            return f"{alias}.json_data::jsonb #> '{{{parts}}}'"

    birth_ref = _person_event_ref_sql(dialect, "birth", treeid)
    death_ref = _person_event_ref_sql(dialect, "death", treeid)
    tree_where = ""
    tree_join = ""
    params: list[Any] = [person, person]
    if treeid is not None:
        tree_where = " AND person.treeid = ?"
        tree_join = " AND {event}.treeid = person.treeid"
        params.append(treeid)
    basedb.dbapi.execute(
        f"""
SELECT 'person', person.handle, person.gramps_id, person.gender,
       {json_value("person", "$.primary_name")},
       {json_value("person", "$.alternate_names")},
       {json_value("person", "$.media_list[0]")},
       {json_value("person", "$.family_list")},
       {json_value("person", "$.parent_family_list[0]")},
       {json_value("birth_event", "$.date")},
       {json_value("death_event", "$.date")},
       NULL, NULL, NULL, NULL, NULL
FROM person
LEFT JOIN event AS birth_event
  ON birth_event.handle = {birth_ref}{tree_join.format(event="birth_event")}
LEFT JOIN event AS death_event
  ON death_event.handle = {death_ref}{tree_join.format(event="death_event")}
WHERE (person.handle = ? OR person.gramps_id = ?){tree_where}
LIMIT 1
""",
        params,
    )
    row = basedb.dbapi.fetchone()
    if row is None:
        return None
    result = _person_graph_projection(row, locale)
    result.pop("_family_handles")
    result.pop("_primary_parent_handle")
    return result


def get_home_person_view(db: Any, person: str) -> dict | None:
    """Return the privacy-safe card projection used for the current user."""
    locale = get_locale_for_language(None, default=True)
    if not isinstance(db, ProxyDbBase):
        return _home_person_projection(db, person, locale)
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
        return None
    item.profile = get_person_profile_for_object(db, item, ["self"], locale=locale)
    return GrampsJSONEncoder().extract_objects(item)


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
        scope = RelationshipScope(
            person=person,
            max_degree=args["degree"],
            direction=args["direction"],
        )
        locale = get_locale_for_language(args["locale"], default=True)

        # A privacy proxy can hide nested private values, not just entire DB
        # rows. Keep that authoritative path for restricted users. Owners can
        # use a single SQL projection that never hydrates complete Gramps
        # Person/Event/Family objects merely to draw a card and a line.
        if not isinstance(db, ProxyDbBase):
            basedb, cte, params = _scope_sql(db, scope)
            rows = _relationship_graph_rows(
                basedb, cte, params, _resolve_dialect(basedb)
            )
            people = [
                _person_graph_projection(row, locale)
                for row in rows
                if row[0] == "person"
            ]
            if not people:
                abort_with_message(404, f"Person {person} not found")
            families = {
                family["handle"]: family
                for row in rows
                if row[0] == "family"
                for family in [_family_graph_projection(row)]
            }
            for item in people:
                item["family_handles"] = item.pop("_family_handles")
                item["primary_parent_family_handle"] = item.pop(
                    "_primary_parent_handle"
                )
            return self.response(
                200, {"people": people, "families": list(families.values())}
            )

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
        return self.response(200, {"person": get_home_person_view(db, person)})


class RecentChangesViewResource(ProtectedResource, GrampsJSONEncoder):
    """Compact, directly queried change feed for the dashboard."""

    @api_blueprint.response(200, SearchResultSchema(many=True))
    @api_blueprint.arguments(RecentChangesArgs, location="query")
    @request_cache_decorator
    def get(self, args: dict) -> Response:
        """Return the most recently changed primary objects."""
        return self.response(
            200,
            get_recent_changes_view(
                get_db_handle(), since=args["since"], limit=args["limit"]
            ),
        )


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
