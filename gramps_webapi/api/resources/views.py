"""SQL-scoped compound responses for expensive frontend modules."""

from __future__ import annotations

import json
from hashlib import sha256
from collections import deque
from datetime import date
from typing import Any

from flask import Response
from gramps.gen.lib.json_utils import data_to_object
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
from .relationship_scope import (
    RelationshipScope,
    compile_connection_path_query,
    compile_primary_ancestors_query,
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
)

VIEW_OBJECT_TYPES = (
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
OBJECT_TYPES_WITH_PRIVACY = frozenset(VIEW_OBJECT_TYPES) - {"tag"}
OBJECT_TYPES_WITH_GRAMPS_ID = frozenset(VIEW_OBJECT_TYPES) - {"tag"}


class RelationshipGraphArgs(Schema):
    """Relationship graph query arguments."""

    degree = fields.Int(load_default=10, validate=validate.Range(min=0, max=12))
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
    type = fields.DelimitedList(fields.Str(validate=validate.OneOf(VIEW_OBJECT_TYPES)))


class ObjectSummariesArgs(Schema):
    """Arguments for compact picker cards identified by type and handle."""

    objects = fields.DelimitedList(
        fields.Str(validate=validate.Length(min=3, max=255)),
        required=True,
        validate=validate.Length(min=1, max=100),
    )
    locale = fields.Str(load_default=None, validate=validate.Length(min=1, max=5))


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


def _type_projection(value: Any) -> Any:
    """Keep only the enum fields used by map relationship helpers."""
    if not isinstance(value, dict):
        return value
    return {key: value.get(key) for key in ("value", "string")}


def _map_projection(object_type: str, item: dict) -> dict:
    """Project raw Gramps JSON to the fields needed by map trajectories."""
    if object_type == "person":
        raw_refs = item.get("event_ref_list", [])
        refs = [
            {"ref": ref.get("ref")}
            for ref in raw_refs
            if not ref.get("private") and ref.get("ref")
        ]
        birth_index = item.get("birth_ref_index")
        birth_ref = (
            raw_refs[birth_index].get("ref")
            if isinstance(birth_index, int) and 0 <= birth_index < len(raw_refs)
            else None
        )
        return {
            "handle": item.get("handle"),
            "event_ref_list": refs,
            "birth_ref_index": next(
                (index for index, ref in enumerate(refs) if ref["ref"] == birth_ref),
                -1,
            ),
            "family_list": item.get("family_list", []),
        }
    if object_type == "family":
        return {
            "handle": item.get("handle"),
            "event_ref_list": [
                {"ref": ref.get("ref")}
                for ref in item.get("event_ref_list", [])
                if not ref.get("private") and ref.get("ref")
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
                if not ref.get("private") and ref.get("ref")
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


def get_recent_changes_view(
    db: Any, *, since: float, limit: int, object_types: list[str] | None = None
) -> list[dict]:
    """Select and project the newest primary objects without a search index."""
    basedb = _base_db(db)
    treeid = _resolve_treeid(basedb)
    include_private = has_permissions({PERM_VIEW_PRIVATE})
    selected_types = tuple(dict.fromkeys(object_types or VIEW_OBJECT_TYPES))
    selects = []
    params: list[Any] = []
    for object_type in selected_types:
        filters = ["change > ?"]
        params.append(since)
        if treeid is not None:
            filters.append("treeid = ?")
            params.append(treeid)
        if not include_private and object_type in OBJECT_TYPES_WITH_PRIVACY:
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
        for object_type in selected_types
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
    return _family_graph_projection_values(
        handle, father, mother, type_raw, children_raw
    )


def _family_graph_projection_values(
    handle: str,
    father: str | None,
    mother: str | None,
    type_raw: Any,
    children_raw: Any,
) -> dict:
    """Build one compact family from SQL columns or a stored JSON record."""
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


def _family_graph_projection_from_item(item: dict) -> dict:
    return _family_graph_projection_values(
        item.get("handle"),
        item.get("father_handle"),
        item.get("mother_handle"),
        item.get("type"),
        item.get("child_ref_list"),
    )


def _projected_step_relationship_type(
    family: dict, from_handle: str, to_handle: str, relation: str
) -> str:
    """Return the edge label without hydrating a Gramps Family object."""
    if relation == "partner":
        return family["type"]
    child_handle = to_handle if relation == "child" else from_handle
    child = next(ref for ref in family["child_ref_list"] if ref["ref"] == child_handle)
    if relation in {"child", "parent"}:
        parent_handle = from_handle if relation == "child" else to_handle
        if parent_handle == family["father_handle"]:
            return child["frel"]
        if parent_handle == family["mother_handle"]:
            return child["mrel"]
        raise ValueError("Parent-child path step has no matching family parent")
    if relation == "sibling":
        other_handle = from_handle if child_handle == to_handle else to_handle
        other = next(
            ref for ref in family["child_ref_list"] if ref["ref"] == other_handle
        )
        rank = {
            "Birth": 0,
            "Adopted": 1,
            "Stepchild": 2,
            "Foster": 3,
            "Sponsored": 4,
            "Other": 5,
            "Custom": 5,
            "None": 6,
            "Unknown": 6,
        }
        candidates = []
        if family["father_handle"]:
            candidates.append((child["frel"], other["frel"]))
        if family["mother_handle"]:
            candidates.append((child["mrel"], other["mrel"]))
        return min(
            (max(pair, key=lambda value: rank.get(value, 5)) for pair in candidates),
            key=lambda value: rank.get(value, 5),
            default="Unknown",
        )
    raise ValueError(f"Unsupported path relation: {relation}")


def _person_event_ref_sql(
    dialect: Any, kind: str, treeid: Any, *, include_private: bool
) -> str:
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
    privacy_clause = "" if include_private else " AND fallback_event.private = 0"
    if dialect.value == "sqlite":
        indexed_privacy = (
            ""
            if include_private
            else " AND COALESCE(json_extract(person.json_data, "
            f"'$.event_ref_list[' || {index} || '].private'), 0) = 0"
        )
        indexed = (
            f"CASE WHEN {index} >= 0{indexed_privacy} THEN "
            "json_extract(person.json_data, "
            f"'$.event_ref_list[' || {index} || '].ref') END"
        )
        ref_privacy = (
            ""
            if include_private
            else " AND COALESCE(json_extract(ref.value, '$.private'), 0) = 0"
        )
        fallback = f"""
SELECT json_extract(ref.value, '$.ref')
FROM json_each(person.json_data, '$.event_ref_list') AS ref
JOIN event AS fallback_event
  ON fallback_event.handle = json_extract(ref.value, '$.ref'){tree_clause}{privacy_clause}
WHERE json_extract(ref.value, '$.role.value') = 1
  {ref_privacy}
  AND json_extract(fallback_event.json_data, '$.type.value') IN ({type_list})
ORDER BY CAST(ref.key AS integer)
LIMIT 1
"""
    else:
        indexed_privacy = (
            ""
            if include_private
            else " AND NOT COALESCE((person.json_data::jsonb -> "
            f"'event_ref_list' -> {index} ->> 'private')::boolean, false)"
        )
        indexed = (
            f"CASE WHEN {index} >= 0{indexed_privacy} THEN person.json_data::jsonb "
            f"-> 'event_ref_list' -> {index} ->> 'ref' END"
        )
        ref_privacy = (
            ""
            if include_private
            else " AND NOT COALESCE((ref.value ->> 'private')::boolean, false)"
        )
        fallback = f"""
SELECT ref.value ->> 'ref'
FROM jsonb_array_elements(
       COALESCE(person.json_data::jsonb -> 'event_ref_list', '[]'::jsonb)
     ) WITH ORDINALITY AS ref(value, ordinal)
JOIN event AS fallback_event
  ON fallback_event.handle = ref.value ->> 'ref'{tree_clause}{privacy_clause}
WHERE (ref.value #>> '{{role,value}}')::integer = 1
  {ref_privacy}
  AND (fallback_event.json_data::jsonb #>> '{{type,value}}')::integer
      IN ({type_list})
ORDER BY ref.ordinal
LIMIT 1
"""
    return f"COALESCE({indexed}, ({fallback.strip()}))"


def get_person_card_summaries(
    db: Any, handles: list[str], locale: Any
) -> dict[str, dict]:
    """Fetch the fields used by person picker/search cards in one SQL query."""
    if not handles:
        return {}
    basedb = _base_db(db)
    dialect = _resolve_dialect(basedb)
    treeid = _resolve_treeid(basedb)
    include_private = has_permissions({PERM_VIEW_PRIVATE})
    placeholders = ", ".join("?" for _ in handles)
    params: list[Any] = list(handles)
    if dialect.value == "sqlite":
        primary = "json_extract(person.json_data, '$.primary_name')"
        birth_date = "json_extract(birth_event.json_data, '$.date')"
        place_name = "json_extract(place.json_data, '$.name.value')"
    else:
        primary = "person.json_data::jsonb -> 'primary_name'"
        birth_date = "birth_event.json_data::jsonb -> 'date'"
        place_name = "place.json_data::jsonb #>> '{name,value}'"
    birth_ref = _person_event_ref_sql(
        dialect, "birth", treeid, include_private=include_private
    )
    tree_person = ""
    tree_event = ""
    tree_place = ""
    if treeid is not None:
        tree_person = " AND person.treeid = ?"
        params.append(treeid)
        tree_event = " AND birth_event.treeid = person.treeid"
        tree_place = " AND place.treeid = person.treeid"
    privacy_person = "" if include_private else " AND person.private = 0"
    privacy_event = "" if include_private else " AND birth_event.private = 0"
    privacy_place = "" if include_private else " AND place.private = 0"
    basedb.dbapi.execute(
        f"""
SELECT person.handle, person.gramps_id, person.gender, person.change,
       {primary}, {birth_date}, {place_name}
FROM person
LEFT JOIN event AS birth_event
  ON birth_event.handle = {birth_ref}{tree_event}{privacy_event}
LEFT JOIN place
  ON place.handle = birth_event.place{tree_place}{privacy_place}
WHERE person.handle IN ({placeholders}){tree_person}{privacy_person}
""",
        params,
    )
    result = {}
    for (
        handle,
        gramps_id,
        gender,
        change,
        primary_raw,
        birth_raw,
        place,
    ) in basedb.dbapi.fetchall():
        name = _project_name(primary_raw)
        birth = _project_date(birth_raw, locale)
        if place:
            birth["place_name"] = place
        result[handle] = {
            "handle": handle,
            "gramps_id": gramps_id,
            "gender": gender,
            "change": change,
            "primary_name": name,
            "profile": {
                "handle": handle,
                "gramps_id": gramps_id,
                "sex": {0: "F", 1: "M", 3: "X"}.get(gender, "U"),
                "birth": birth,
                "name_given": name.get("first_name") or "",
                "name_surname": _surname(name),
                "name_suffix": name.get("suffix") or "",
                "name_title": name.get("title") or "",
            },
        }
    return result


def get_object_summaries_view(
    db: Any, references: list[tuple[str, str]], locale: Any
) -> list[dict]:
    """Return compact cards for typed handles, preserving the requested order."""
    basedb = _base_db(db)
    treeid = _resolve_treeid(basedb)
    include_private = has_permissions({PERM_VIEW_PRIVATE})
    grouped: dict[str, list[str]] = {}
    for object_type, handle in references:
        grouped.setdefault(object_type, []).append(handle)

    items: dict[tuple[str, str], dict] = {}
    for object_type, identifiers in grouped.items():
        placeholders = ", ".join("?" for _ in identifiers)
        params: list[Any] = list(identifiers)
        filters = [f"handle IN ({placeholders})"]
        gramps_id_column = "NULL AS gramps_id"
        if object_type in OBJECT_TYPES_WITH_GRAMPS_ID:
            filters[0] = f"({filters[0]} OR gramps_id IN ({placeholders}))"
            params.extend(identifiers)
            gramps_id_column = "gramps_id"
        if treeid is not None:
            filters.append("treeid = ?")
            params.append(treeid)
        if not include_private and object_type in OBJECT_TYPES_WITH_PRIVACY:
            filters.append("private = 0")
        basedb.dbapi.execute(
            f"SELECT handle, {gramps_id_column}, json_data FROM {object_type} "
            f"WHERE {' AND '.join(filters)}",
            params,
        )
        for handle, gramps_id, raw in basedb.dbapi.fetchall():
            item = _json_fragment(raw, {})
            items[(object_type, handle)] = item
            items[(object_type, gramps_id)] = item

    person_handles = list(
        dict.fromkeys(
            items[(object_type, identifier)].get("handle")
            for object_type, identifier in references
            if object_type == "person" and (object_type, identifier) in items
        )
    )
    person_summaries = get_person_card_summaries(db, person_handles, locale)
    family_people = {
        handle
        for (object_type, _), item in items.items()
        if object_type == "family"
        for handle in (item.get("father_handle"), item.get("mother_handle"))
        if handle
    }
    citation_sources = {
        item.get("source_handle")
        for (object_type, _), item in items.items()
        if object_type == "citation" and item.get("source_handle")
    }
    people = _recent_related_objects(
        basedb, "person", family_people, include_private=include_private
    )
    sources = _recent_related_objects(
        basedb, "source", citation_sources, include_private=include_private
    )

    result = []
    for object_type, identifier in references:
        item = items.get((object_type, identifier))
        if item is None:
            continue
        handle = item.get("handle")
        projection = person_summaries.get(handle) or _recent_projection(
            object_type, item, people, sources
        )
        result.append(
            {"handle": handle, "object_type": object_type, "object": projection}
        )
    return result


def _relationship_graph_rows(
    basedb: Any,
    cte: str,
    params: list[Any],
    dialect: Any,
    *,
    include_private: bool,
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
    birth_ref = _person_event_ref_sql(
        dialect, "birth", treeid, include_private=include_private
    )
    death_ref = _person_event_ref_sql(
        dialect, "death", treeid, include_private=include_private
    )
    tree_join = ""
    if treeid is not None:
        tree_join = " AND {event}.treeid = person.treeid"
    event_privacy = "" if include_private else " AND {event}.private = 0"
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
LEFT JOIN event AS birth_event ON birth_event.handle = {birth_ref}{tree_join.format(event="birth_event")}{event_privacy.format(event="birth_event")}
LEFT JOIN event AS death_event ON death_event.handle = {death_ref}{tree_join.format(event="death_event")}{event_privacy.format(event="death_event")}
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


def _selected_graph_rows(
    db: Any,
    person_handles: list[str],
    family_handles: list[str],
    *,
    include_private: bool,
) -> list[tuple]:
    """Fetch compact graph projections for two already selected handle sets."""
    basedb = _base_db(db)
    treeid = _resolve_treeid(basedb)

    def relation(table: str, handles: list[str]) -> tuple[str, list[Any]]:
        if not handles:
            return f"SELECT handle FROM {table} WHERE 1 = 0", []
        placeholders = ", ".join("?" for _ in handles)
        filters = [f"handle IN ({placeholders})"]
        params: list[Any] = list(handles)
        if treeid is not None:
            filters.append("treeid = ?")
            params.append(treeid)
        if not include_private:
            filters.append("private = 0")
        return f"SELECT handle FROM {table} WHERE {' AND '.join(filters)}", params

    people_sql, people_params = relation("person", person_handles)
    families_sql, family_params = relation("family", family_handles)
    cte = f"""
WITH relationship_scope_persons AS ({people_sql}),
relationship_scope_families AS ({families_sql})
""".strip()
    return _relationship_graph_rows(
        basedb,
        cte,
        people_params + family_params,
        _resolve_dialect(basedb),
        include_private=include_private,
    )


def _home_person_projection(
    db: Any, person: str, locale: Any, *, include_private: bool
) -> dict | None:
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

    birth_ref = _person_event_ref_sql(
        dialect, "birth", treeid, include_private=include_private
    )
    death_ref = _person_event_ref_sql(
        dialect, "death", treeid, include_private=include_private
    )
    tree_where = ""
    tree_join = ""
    params: list[Any] = [person, person]
    if treeid is not None:
        tree_where = " AND person.treeid = ?"
        tree_join = " AND {event}.treeid = person.treeid"
        params.append(treeid)
    person_privacy = "" if include_private else " AND person.private = 0"
    event_privacy = "" if include_private else " AND {event}.private = 0"
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
  ON birth_event.handle = {birth_ref}{tree_join.format(event="birth_event")}{event_privacy.format(event="birth_event")}
LEFT JOIN event AS death_event
  ON death_event.handle = {death_ref}{tree_join.format(event="death_event")}{event_privacy.format(event="death_event")}
WHERE (person.handle = ? OR person.gramps_id = ?){tree_where}{person_privacy}
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
    return _home_person_projection(
        db,
        person,
        locale,
        include_private=has_permissions({PERM_VIEW_PRIVATE}),
    )


class RelationshipGraphViewResource(ProtectedResource, GrampsJSONEncoder):
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

        include_private = has_permissions({PERM_VIEW_PRIVATE})
        basedb, cte, params = _scope_sql(db, scope)
        rows = _relationship_graph_rows(
            basedb,
            cte,
            params,
            _resolve_dialect(basedb),
            include_private=include_private,
        )
        people = [
            _person_graph_projection(row, locale) for row in rows if row[0] == "person"
        ]
        if not people:
            abort_with_message(404, f"Person {person} not found")
        families = {
            family["handle"]: family
            for row in rows
            if row[0] == "family"
            for family in [_family_graph_projection(row)]
        }
        visible_people = {item["handle"] for item in people}
        visible_families = set(families)
        for item in people:
            item["family_handles"] = [
                handle
                for handle in item.pop("_family_handles")
                if handle in visible_families
            ]
            parent = item.pop("_primary_parent_handle")
            item["primary_parent_family_handle"] = (
                parent if parent in visible_families else ""
            )
        for family in families.values():
            if family["father_handle"] not in visible_people:
                family["father_handle"] = ""
            if family["mother_handle"] not in visible_people:
                family["mother_handle"] = ""
            family["child_ref_list"] = [
                ref for ref in family["child_ref_list"] if ref["ref"] in visible_people
            ]
        return self.response(
            200, {"people": people, "families": list(families.values())}
        )


class HomePersonViewResource(ProtectedResource, GrampsJSONEncoder):
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
                get_db_handle(),
                since=args["since"],
                limit=args["limit"],
                object_types=args.get("type"),
            ),
        )


class ObjectSummariesViewResource(ProtectedResource, GrampsJSONEncoder):
    """Compact card projections for picker history and bookmarks."""

    @api_blueprint.response(200, SearchResultSchema(many=True))
    @api_blueprint.arguments(ObjectSummariesArgs, location="query")
    @request_cache_decorator
    def get(self, args: dict) -> Response:
        """Resolve typed handles without full-object API requests."""
        references = []
        for value in args["objects"]:
            object_type, separator, handle = value.partition(":")
            if not separator or object_type not in VIEW_OBJECT_TYPES or not handle:
                abort_with_message(422, f"Invalid object reference: {value}")
            pair = (object_type, handle)
            if pair not in references:
                references.append(pair)
        locale = get_locale_for_language(args["locale"], default=True)
        return self.response(
            200, get_object_summaries_view(get_db_handle(), references, locale)
        )


class ConnectionGraphViewResource(ProtectedResource, GrampsJSONEncoder):
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
        include_private = has_permissions({PERM_VIEW_PRIVATE})
        family_items = _recent_related_objects(
            basedb,
            "family",
            set(family_handles),
            include_private=include_private,
        )
        families_by_handle = {
            handle: _family_graph_projection_from_item(item)
            for handle, item in family_items.items()
        }

        for step in steps:
            family = families_by_handle[step["family_handle"]]
            step["relationship_type"] = _projected_step_relationship_type(
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
            for handle in (family["father_handle"], family["mother_handle"]):
                if (
                    handle
                    and handle not in path_handle_set
                    and handle not in person_handles
                ):
                    person_handles.append(handle)

        locale = get_locale_for_language(args["locale"], default=True)
        graph_rows = _selected_graph_rows(
            db,
            person_handles,
            family_handles,
            include_private=include_private,
        )
        people = [
            _person_graph_projection(row, locale)
            for row in graph_rows
            if row[0] == "person"
        ]
        visible_people = {person["handle"] for person in people}
        visible_families = set(families_by_handle)
        for person in people:
            person["family_handles"] = [
                handle
                for handle in person.pop("_family_handles")
                if handle in visible_families
            ]
            parent = person.pop("_primary_parent_handle")
            person["primary_parent_family_handle"] = (
                parent if parent in visible_families else ""
            )
        families = []
        for handle in family_handles:
            family = families_by_handle[handle]
            if family["father_handle"] not in visible_people:
                family["father_handle"] = ""
            if family["mother_handle"] not in visible_people:
                family["mother_handle"] = ""
            family["child_ref_list"] = [
                ref for ref in family["child_ref_list"] if ref["ref"] in visible_people
            ]
            families.append(family)

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
        for object_type, handle, raw in basedb.dbapi.fetchall():
            item = _json_fragment(raw, {})
            objects[response_keys[object_type]].append(
                _map_projection(object_type, item)
            )
        if not objects["people"]:
            abort_with_message(404, f"Person {person} not found")
        if not has_permissions({PERM_VIEW_PRIVATE}):
            visible_people = {item["handle"] for item in objects["people"]}
            visible_families = {item["handle"] for item in objects["families"]}
            for person_item in objects["people"]:
                person_item["family_list"] = [
                    handle
                    for handle in person_item["family_list"]
                    if handle in visible_families
                ]
            for family in objects["families"]:
                if family["father_handle"] not in visible_people:
                    family["father_handle"] = ""
                if family["mother_handle"] not in visible_people:
                    family["mother_handle"] = ""
                family["child_ref_list"] = [
                    ref
                    for ref in family["child_ref_list"]
                    if ref["ref"] in visible_people
                ]
            place_handles = {
                item["place"] for item in objects["events"] if item.get("place")
            }
            visible_places = set(
                _recent_related_objects(
                    basedb, "place", place_handles, include_private=False
                )
            )
            for event in objects["events"]:
                if event.get("place") not in visible_places:
                    event["place"] = ""
        return self.response(200, objects)


class AncestorOfTheDayArgs(ConnectionGraphArgs):
    date = fields.Date(required=True)


def daily_ancestor(handles: list[str], day: date, person: str) -> str | None:
    """Stable date-based ranking, independent of query order and Python hash seed."""
    return min(
        handles,
        key=lambda handle: sha256(
            f"{day.isoformat()}:{person}:{handle}".encode()
        ).digest(),
        default=None,
    )


class AncestorOfTheDayViewResource(ProtectedResource, GrampsJSONEncoder):
    """A personalized daily card without fetching the full ancestry graph."""

    @api_blueprint.response(200, HomePersonResponse())
    @api_blueprint.arguments(AncestorOfTheDayArgs, location="query")
    @request_cache_decorator
    def get(self, args: dict, person: str) -> Response:
        db = get_db_handle()
        basedb = _base_db(db)
        include_private = has_permissions({PERM_VIEW_PRIVATE})
        locale = get_locale_for_language(args["locale"], default=True)
        home = _home_person_projection(
            db, person, locale, include_private=include_private
        )
        if home is None:
            return self.response(200, {"person": None})
        sql, params = compile_primary_ancestors_query(
            home["handle"],
            dialect=_resolve_dialect(basedb),
            treeid=_resolve_treeid(basedb),
            include_private=include_private,
        )
        basedb.dbapi.execute(sql, params)
        selected = daily_ancestor(
            [row[0] for row in basedb.dbapi.fetchall()], args["date"], home["handle"]
        )
        ancestor = (
            _home_person_projection(
                db, selected, locale, include_private=include_private
            )
            if selected
            else None
        )
        return self.response(200, {"person": ancestor})
