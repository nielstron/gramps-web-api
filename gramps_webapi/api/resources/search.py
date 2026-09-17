#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2020      David Straub
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

"""Full-text search endpoint."""

from __future__ import annotations

import re
from typing import Dict, Optional

from flask import Response
from flask_jwt_extended import get_jwt_identity
from gramps.gen.db.base import DbReadBase
from gramps.gen.errors import HandleError
from gramps.gen.lib.primaryobj import BasicPrimaryObject as GrampsObject
from gramps.gen.proxy.proxybase import ProxyDbBase
from gramps.gen.utils.grampslocale import GrampsLocale
from marshmallow import Schema
from webargs import fields, validate

from ...auth.const import PERM_TRIGGER_REINDEX, PERM_VIEW_PRIVATE
from ...const import PRIMARY_GRAMPS_OBJECTS
from ..auth import has_permissions, require_permissions
from ..blueprint import api_blueprint
from ..search import (
    SearchIndexer,
    SemanticSearchIndexer,
    get_search_indexer,
    get_semantic_search_indexer,
)
from ..tasks import (
    AsyncResult,
    make_task_response,
    run_task,
    search_reindex_full,
    search_reindex_full_semantic,
    search_reindex_incremental,
    search_reindex_incremental_semantic,
)
from ..util import (
    get_db_handle,
    get_locale_for_language,
    get_tree_from_jwt_or_fail,
)
from . import ProtectedResource
from .emit import GrampsJSONEncoder
from .schemas import SearchResultSchema
from .util import (
    abort_with_message,
    get_citation_profile_for_object,
    get_event_profile_for_object,
    get_family_profile_for_object,
    get_media_profile_for_object,
    get_person_profile_for_object,
    get_place_profile_for_object,
    get_repository_profile_for_object,
)
from .views import (
    _base_db,
    _person_event_ref_sql,
    _project_date,
    _project_name,
    _resolve_dialect,
    _resolve_treeid,
    _surname,
)


class SearchQueryArgs(Schema):
    """Query arguments for GET /search/."""

    locale = fields.Str(
        load_default=None,
        validate=validate.Length(min=1, max=5),
        metadata={
            "description": "Language code of the locale to use where applicable. Must be a valid code from the available translations."
        },
    )
    query = fields.Str(
        required=True,
        validate=validate.Length(min=1),
        metadata={"description": "The search string."},
    )
    semantic = fields.Boolean(
        load_default=False,
        metadata={
            "description": "If true, use semantic (vector) search rather than full-text search."
        },
    )
    page = fields.Int(
        load_default=1,
        validate=validate.Range(min=1),
        metadata={"description": "Page number of the result subset to return."},
    )
    pagesize = fields.Int(
        load_default=20,
        validate=validate.Range(min=1),
        metadata={"description": "Number of search results per page."},
    )
    sort = fields.DelimitedList(
        fields.Str(validate=validate.Length(min=1)),
        metadata={
            "description": "Comma-delimited sort keys for search results. Available: change, type. Prefix with '-' for descending."
        },
    )
    precision = fields.Integer(
        load_default=3,
        validate=validate.Range(min=1, max=3),
        metadata={
            "description": "Number of significant time components in age/span"
            " strings when profile is used: 1=year only, 2=year+month,"
            " 3=year+month+day."
        },
    )
    profile = fields.DelimitedList(
        fields.Str(validate=validate.Length(min=1)),
        validate=validate.ContainsOnly(
            choices=["all", "self", "families", "events", "age", "span"]
        ),
        metadata={
            "description": "Comma-delimited profile sections to include for matching objects. Possible values: all, age, self, span, events, families, references."
        },
    )
    strip = fields.Boolean(
        load_default=False,
        metadata={
            "description": "If true, strip keys with empty values from the response."
        },
    )
    summary = fields.Boolean(
        load_default=False,
        metadata={
            "description": "Return the compact object projection used by search result cards."
        },
    )
    type = fields.DelimitedList(
        fields.Str(validate=validate.Length(min=1)),
        validate=validate.ContainsOnly(
            choices=[t.lower() for t in PRIMARY_GRAMPS_OBJECTS]
        ),
        metadata={
            "description": "Comma-delimited list of object types to include (e.g. 'person,family,source'). Results are grouped in the requested type order before pagination."
        },
    )
    change = fields.Str(
        validate=validate.Length(min=2),
        metadata={
            "description": "ISO-8601 timestamp filter (prefix with '>' or '<') to filter by last-change date."
        },
    )


class SearchResource(GrampsJSONEncoder, ProtectedResource):
    """Fulltext search resource."""

    @property
    def db_handle(self) -> DbReadBase:
        """Get the database instance."""
        return get_db_handle()

    def get_object_from_handle(
        self, handle: str, class_name: str, args: Dict, locale: GrampsLocale
    ) -> GrampsObject:
        """Get the object given a Gramps handle."""
        query_method = self.db_handle.method("get_%s_from_handle", class_name)
        assert query_method is not None  # type checker
        obj = query_method(handle)
        if obj is None:
            raise HandleError(f"Object not found for handle {handle}")
        if "profile" in args:
            if class_name == "person":
                obj.profile = get_person_profile_for_object(
                    self.db_handle,
                    obj,
                    args["profile"],
                    locale=locale,
                    name_format=args.get("name_format"),
                    precision=args.get("precision", 3),
                )
            elif class_name == "family":
                obj.profile = get_family_profile_for_object(
                    self.db_handle,
                    obj,
                    args["profile"],
                    locale=locale,
                    name_format=args.get("name_format"),
                    precision=args.get("precision", 3),
                )
            elif class_name == "event":
                obj.profile = get_event_profile_for_object(
                    self.db_handle,
                    obj,
                    args["profile"],
                    locale=locale,
                    name_format=args.get("name_format"),
                    precision=args.get("precision", 3),
                )
            elif class_name == "citation":
                obj.profile = get_citation_profile_for_object(
                    self.db_handle, obj, args["profile"], locale=locale
                )
            elif class_name == "place":
                obj.profile = get_place_profile_for_object(
                    self.db_handle, obj, locale=locale
                )
            elif class_name == "media":
                obj.profile = get_media_profile_for_object(
                    self.db_handle, obj, args["profile"], locale=locale
                )
            elif class_name == "repository":
                obj.profile = get_repository_profile_for_object(
                    self.db_handle, obj, args["profile"], locale=locale
                )

        return obj

    def get_person_summaries(
        self, handles: list[str], locale: GrampsLocale
    ) -> dict[str, dict]:
        """Fetch the fields used by person search cards in one SQL query."""
        if not handles or isinstance(self.db_handle, ProxyDbBase):
            return {}
        basedb = _base_db(self.db_handle)
        dialect = _resolve_dialect(basedb)
        treeid = _resolve_treeid(basedb)
        placeholders = ", ".join("?" for _ in handles)
        params: list = list(handles)
        if dialect.value == "sqlite":
            primary = "json_extract(person.json_data, '$.primary_name')"
            birth_date = "json_extract(birth_event.json_data, '$.date')"
            place_name = "json_extract(place.json_data, '$.name.value')"
        else:
            primary = "person.json_data::jsonb -> 'primary_name'"
            birth_date = "birth_event.json_data::jsonb -> 'date'"
            place_name = "place.json_data::jsonb #>> '{name,value}'"
        birth_ref = _person_event_ref_sql(dialect, "birth", treeid)
        tree_person = ""
        tree_event = ""
        tree_place = ""
        if treeid is not None:
            tree_person = " AND person.treeid = ?"
            params.append(treeid)
            tree_event = " AND birth_event.treeid = person.treeid"
            tree_place = " AND place.treeid = person.treeid"
        basedb.dbapi.execute(
            f"""
SELECT person.handle, person.gramps_id, person.gender, person.change,
       {primary}, {birth_date}, {place_name}
FROM person
LEFT JOIN event AS birth_event
  ON birth_event.handle = {birth_ref}{tree_event}
LEFT JOIN place
  ON place.handle = birth_event.place{tree_place}
WHERE person.handle IN ({placeholders}){tree_person}
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

    @api_blueprint.response(200, SearchResultSchema(many=True))
    @api_blueprint.arguments(SearchQueryArgs, location="query")
    def get(self, args: Dict):
        """Get search result."""
        tree = get_tree_from_jwt_or_fail()
        try:
            if args["semantic"]:
                searcher: SearchIndexer | SemanticSearchIndexer = (
                    get_semantic_search_indexer(tree)
                )
            else:
                searcher = get_search_indexer(tree)
        except ValueError as exc:
            abort_with_message(503, str(exc))
        if args["semantic"] and args.get("sort"):
            abort_with_message(
                422, "the sort parameter is not allowed with semantic search"
            )
        if args.get("change"):
            match = re.match(r"^(<=|>=|<|>)(\d+(\.\d+)?)$", args["change"])
            if match:
                change_op: Optional[str] = match.group(1)
                change_value: Optional[float] = float(match.group(2))
            else:
                abort_with_message(422, "change parameter has invalid format")
        else:
            change_op = None
            change_value = None

        total, hits = searcher.search(
            query=args["query"],
            page=args["page"],
            pagesize=args["pagesize"],
            # search in private records if allowed to
            include_private=has_permissions([PERM_VIEW_PRIVATE]),
            sort=args.get("sort"),
            object_types=args.get("type") or None,
            change_op=change_op,
            change_value=change_value,
        )
        if hits:
            locale = get_locale_for_language(args["locale"], default=True)
            person_summaries = (
                self.get_person_summaries(
                    [hit["handle"] for hit in hits if hit["object_type"] == "person"],
                    locale,
                )
                if args["summary"]
                else {}
            )
            for hit in hits:
                try:
                    if hit["handle"] in person_summaries:
                        hit["object"] = person_summaries[hit["handle"]]
                        continue
                    object_args = args
                    if args["summary"] and "profile" not in args:
                        object_args = {**args, "profile": ["self"]}
                    hit["object"] = self.get_object_from_handle(
                        handle=hit["handle"],
                        class_name=hit["object_type"],
                        args=object_args,
                        locale=locale,
                    )
                except HandleError:
                    pass
            # filter out hits without object (i.e. if handle failed)
            hits = [hit for hit in hits if "object" in hit]
        return self.response(200, payload=hits or [], args=args, total_items=total)


class SearchIndexQueryArgs(Schema):
    """Query arguments for POST /search/index/."""

    full = fields.Boolean(
        load_default=False,
        metadata={
            "description": "If true, perform a full reindex; otherwise incremental."
        },
    )
    semantic = fields.Boolean(
        load_default=False,
        metadata={
            "description": "If true, use semantic (vector) search rather than full-text search."
        },
    )


class SearchIndexResource(ProtectedResource):
    """Resource to trigger a search reindex."""

    @api_blueprint.arguments(SearchIndexQueryArgs, location="query")
    def post(self, args: Dict):
        """Trigger a reindex."""
        require_permissions([PERM_TRIGGER_REINDEX])
        tree = get_tree_from_jwt_or_fail()
        user_id = get_jwt_identity()
        if args["full"]:
            task_func = (
                search_reindex_full_semantic
                if args["semantic"]
                else search_reindex_full
            )
        else:
            task_func = (
                search_reindex_incremental_semantic
                if args["semantic"]
                else search_reindex_incremental
            )
        task = run_task(task_func, tree=tree, user_id=user_id)
        if isinstance(task, AsyncResult):
            return make_task_response(task)
        return Response(status=201)
