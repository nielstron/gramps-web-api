"""Compile reusable relationship scopes for structured object queries.

The scope is deliberately expressed as SQL CTEs rather than a Python graph:
the database selects the bounded family network and exposes the resulting
people, families, and events as three reusable relations.  Object query
resources only add a membership predicate for their own table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from gramps_object_query_language.query import Dialect, ObjectTypeSpec, QueryError

SCOPE_RELATIONS = {"person": "persons", "family": "families", "event": "events"}


def relationship_scope_relation(table: str) -> str:
    """Return the scope relation name for an object table."""

    try:
        return SCOPE_RELATIONS[table]
    except KeyError as error:
        raise QueryError(
            f"relationship scopes are not supported for {table!r} queries"
        ) from error


@dataclass(frozen=True)
class RelationshipScope:
    """A bounded family-network traversal rooted at one person."""

    person: str
    max_degree: int = 4
    direction: str = "any"

    def __post_init__(self) -> None:
        if not self.person:
            raise QueryError("relationship scope requires a person")
        if self.max_degree < 0:
            raise QueryError("relationship scope max_degree must be non-negative")
        if self.direction not in {"any", "ancestors", "descendants"}:
            raise QueryError(
                "relationship scope direction must be any, ancestors, or descendants"
            )


class RelationshipScopePredicate:
    """Query-language-compatible membership predicate for a scope CTE."""

    def compile(
        self,
        spec: ObjectTypeSpec,
        dialect: Optional[Dialect] = None,
        treeid: Optional[int] = None,
    ) -> tuple[str, list]:
        relation = relationship_scope_relation(spec.table)
        return (
            f"{spec.table}.handle IN "
            f"(SELECT handle FROM relationship_scope_{relation})",
            [],
        )


def _table_condition(
    alias: str, treeid: Optional[int], include_private: bool
) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    if treeid is not None:
        clauses.append(f"{alias}.treeid = ?")
        params.append(treeid)
    if not include_private:
        clauses.append(f"{alias}.private = 0")
    return (" AND ".join(clauses) or "1 = 1"), params


def _reference_condition(alias: str, treeid: Optional[int]) -> tuple[str, list[Any]]:
    if treeid is None:
        return "1 = 1", []
    return f"{alias}.treeid = ?", [treeid]


def compile_relationship_scope(
    scope: RelationshipScope,
    *,
    dialect: Dialect,
    treeid: Optional[int],
    include_private: bool,
) -> tuple[str, list[Any]]:
    """Return a recursive CTE prefix and its positional parameters.

    Siblings are intentionally not emitted as direct edges.  They are
    reached through a shared parent in two steps, matching genealogical
    degree semantics.  Partners and each parent-child hop cost one.
    """

    person_where, person_params = _table_condition("p", treeid, include_private)
    family_where, family_params = _table_condition("f", treeid, include_private)
    event_where, event_params = _table_condition("e", treeid, include_private)
    reference_where_person, reference_person_params = _reference_condition("rp", treeid)
    reference_where_family, reference_family_params = _reference_condition("rf", treeid)
    person_record_tree = (
        " AND person_record.treeid = rp.treeid" if treeid is not None else ""
    )

    if dialect == Dialect.SQLITE:
        child_privacy = (
            " WHERE COALESCE(json_extract(child.value, '$.private'), 0) = 0"
            if not include_private
            else ""
        )
        children = (
            "SELECT f.handle AS family_handle, f.father_handle, f.mother_handle, "
            "json_extract(child.value, '$.ref') AS child_handle "
            "FROM relationship_visible_families AS f "
            "JOIN json_each(f.json_data, '$.child_ref_list') AS child"
            f"{child_privacy}"
        )
        person_event_privacy = (
            "AND EXISTS (SELECT 1 FROM json_each("
            "person_record.json_data, '$.event_ref_list') AS event_ref "
            "WHERE json_extract(event_ref.value, '$.ref') = rp.ref_handle "
            "AND COALESCE(json_extract(event_ref.value, '$.private'), 0) = 0)"
            if not include_private
            else ""
        )
        family_event_privacy = (
            "AND EXISTS (SELECT 1 FROM json_each("
            "family.json_data, '$.event_ref_list') AS event_ref "
            "WHERE json_extract(event_ref.value, '$.ref') = rf.ref_handle "
            "AND COALESCE(json_extract(event_ref.value, '$.private'), 0) = 0)"
            if not include_private
            else ""
        )
    elif dialect == Dialect.POSTGRESQL:
        child_privacy = (
            " WHERE NOT COALESCE((child.value ->> 'private')::boolean, false)"
            if not include_private
            else ""
        )
        children = (
            "SELECT f.handle AS family_handle, f.father_handle, f.mother_handle, "
            "child.value ->> 'ref' AS child_handle "
            "FROM relationship_visible_families AS f "
            "CROSS JOIN LATERAL jsonb_array_elements("
            "COALESCE(f.json_data::jsonb -> 'child_ref_list', '[]'::jsonb)"
            ") AS child(value)"
            f"{child_privacy}"
        )
        person_event_privacy = (
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE("
            "person_record.json_data::jsonb -> 'event_ref_list', '[]'::jsonb)) "
            "AS event_ref(value) WHERE event_ref.value ->> 'ref' = rp.ref_handle "
            "AND NOT COALESCE((event_ref.value ->> 'private')::boolean, false))"
            if not include_private
            else ""
        )
        family_event_privacy = (
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE("
            "family.json_data::jsonb -> 'event_ref_list', '[]'::jsonb)) "
            "AS event_ref(value) WHERE event_ref.value ->> 'ref' = rf.ref_handle "
            "AND NOT COALESCE((event_ref.value ->> 'private')::boolean, false))"
            if not include_private
            else ""
        )
    else:
        raise QueryError(f"relationship scopes do not support dialect {dialect!r}")

    parent_child_forward = (
        "SELECT family_handle, father_handle AS from_handle, "
        "child_handle AS to_handle, 'child' AS relation "
        "FROM relationship_children WHERE father_handle IS NOT NULL "
        "AND father_handle != '' "
        "UNION ALL "
        "SELECT family_handle, mother_handle, child_handle, 'child' "
        "FROM relationship_children WHERE mother_handle IS NOT NULL "
        "AND mother_handle != ''"
    )
    parent_child_reverse = (
        "SELECT family_handle, child_handle AS from_handle, "
        "father_handle AS to_handle, 'parent' AS relation "
        "FROM relationship_children WHERE father_handle IS NOT NULL "
        "AND father_handle != '' "
        "UNION ALL "
        "SELECT family_handle, child_handle, mother_handle, 'parent' "
        "FROM relationship_children WHERE mother_handle IS NOT NULL "
        "AND mother_handle != ''"
    )
    partners = (
        "SELECT handle, father_handle, mother_handle, 'partner' "
        "FROM relationship_visible_families "
        "WHERE father_handle IS NOT NULL AND father_handle != '' "
        "AND mother_handle IS NOT NULL AND mother_handle != '' "
        "UNION ALL "
        "SELECT handle, mother_handle, father_handle, 'partner' "
        "FROM relationship_visible_families "
        "WHERE father_handle IS NOT NULL AND father_handle != '' "
        "AND mother_handle IS NOT NULL AND mother_handle != ''"
    )
    if scope.direction == "ancestors":
        edges = parent_child_reverse
    elif scope.direction == "descendants":
        edges = parent_child_forward
    else:
        edges = (
            f"{parent_child_forward} UNION ALL {parent_child_reverse} "
            f"UNION ALL {partners}"
        )

    # SQLite otherwise tends to inline the JSON-expanded edge CTE into every
    # recursive step.  Materializing the small, fixed edge relation once is
    # substantially faster for deep trees.  Filtering both endpoints here
    # also means the recursion cannot cross a private person.
    edge_materialization = " AS MATERIALIZED" if dialect == Dialect.SQLITE else " AS"

    sql = f"""
WITH RECURSIVE
relationship_visible_people AS (
    SELECT p.handle, p.gramps_id
    FROM person AS p
    WHERE {person_where}
),
relationship_visible_families AS (
    SELECT f.handle, f.father_handle, f.mother_handle, f.json_data
    FROM family AS f
    WHERE {family_where}
),
relationship_children AS (
    {children}
),
relationship_edges_raw{edge_materialization} (
    {edges}
),
relationship_edges{edge_materialization} (
    SELECT edge.family_handle, edge.from_handle, edge.to_handle, edge.relation
    FROM relationship_edges_raw AS edge
    JOIN relationship_visible_people AS source ON source.handle = edge.from_handle
    JOIN relationship_visible_people AS target ON target.handle = edge.to_handle
),
relationship_sibling_edges_raw{edge_materialization} (
    SELECT first.family_handle, first.child_handle AS from_handle,
           second.child_handle AS to_handle, 'sibling' AS relation
    FROM relationship_children AS first
    JOIN relationship_children AS second
      ON second.family_handle = first.family_handle
     AND second.child_handle != first.child_handle
),
relationship_connection_edges{edge_materialization} (
    SELECT family_handle, from_handle, to_handle, relation
    FROM relationship_edges
    UNION ALL
    SELECT edge.family_handle, edge.from_handle, edge.to_handle, edge.relation
    FROM relationship_sibling_edges_raw AS edge
    JOIN relationship_visible_people AS source ON source.handle = edge.from_handle
    JOIN relationship_visible_people AS target ON target.handle = edge.to_handle
),
relationship_walk(handle, distance) AS (
    SELECT handle, 0
    FROM relationship_visible_people
    WHERE handle = ? OR gramps_id = ?
    UNION
    SELECT edge.to_handle, walk.distance + 1
    FROM relationship_walk AS walk
    JOIN relationship_edges AS edge ON edge.from_handle = walk.handle
    WHERE walk.distance < ?
),
relationship_scope_persons AS (
    SELECT handle, MIN(distance) AS distance
    FROM relationship_walk
    GROUP BY handle
),
relationship_scope_families AS (
    SELECT DISTINCT family.handle
    FROM relationship_visible_families AS family
    LEFT JOIN relationship_children AS child ON child.family_handle = family.handle
    WHERE family.father_handle IN (SELECT handle FROM relationship_scope_persons)
       OR family.mother_handle IN (SELECT handle FROM relationship_scope_persons)
       OR child.child_handle IN (SELECT handle FROM relationship_scope_persons)
),
relationship_scope_events AS (
    SELECT DISTINCT rp.ref_handle AS handle
    FROM reference AS rp
    JOIN relationship_scope_persons AS person ON person.handle = rp.obj_handle
    JOIN person AS person_record
      ON person_record.handle = person.handle{person_record_tree}
    JOIN event AS e ON e.handle = rp.ref_handle
    WHERE rp.obj_class = 'Person' AND rp.ref_class = 'Event'
      AND {reference_where_person} AND {event_where} {person_event_privacy}
    UNION
    SELECT DISTINCT rf.ref_handle AS handle
    FROM reference AS rf
    JOIN relationship_visible_families AS family ON family.handle = rf.obj_handle
    JOIN relationship_scope_persons AS participant
      ON participant.handle = family.father_handle
      OR participant.handle = family.mother_handle
    JOIN event AS e ON e.handle = rf.ref_handle
    WHERE rf.obj_class = 'Family' AND rf.ref_class = 'Event'
      AND {reference_where_family} AND {event_where} {family_event_privacy}
)
""".strip()
    params = (
        person_params
        + family_params
        + [scope.person, scope.person, scope.max_degree]
        + reference_person_params
        + event_params
        + reference_family_params
        + event_params
    )
    return sql, params


def prefix_relationship_scope(
    sql: str,
    params: list[Any],
    scope: RelationshipScope,
    *,
    dialect: Dialect,
    treeid: Optional[int],
    include_private: bool,
) -> tuple[str, list[Any]]:
    """Prefix a compiled object query with its parameterized scope CTE."""

    cte, cte_params = compile_relationship_scope(
        scope,
        dialect=dialect,
        treeid=treeid,
        include_private=include_private,
    )
    return f"{cte}\n{sql}", cte_params + params


def compile_connection_path_query(
    source: str,
    target: str,
    *,
    dialect: Dialect,
    treeid: Optional[int],
    include_private: bool,
) -> tuple[str, list[Any]]:
    """Compile the connected SQL edge component for two people.

    SQL recursively limits the graph to the source's connected component.  A
    caller can run a cheap breadth-first search over these flat edge rows to
    reconstruct a shortest path without loading or deserializing Gramps
    objects for the rest of the tree.
    """
    scope = RelationshipScope(source, max_degree=0)
    cte, params = compile_relationship_scope(
        scope,
        dialect=dialect,
        treeid=treeid,
        include_private=include_private,
    )
    sql = f"""
{cte},
connection_source AS (
    SELECT handle
    FROM relationship_visible_people
    WHERE handle = ? OR gramps_id = ?
),
connection_target AS (
    SELECT handle
    FROM relationship_visible_people
    WHERE handle = ? OR gramps_id = ?
),
connection_reachable(handle) AS (
    SELECT handle FROM connection_source
    UNION
    SELECT edge.to_handle
    FROM connection_reachable AS reachable
    JOIN relationship_connection_edges AS edge
      ON edge.from_handle = reachable.handle
)
SELECT 'source' AS kind, source.handle, 0 AS distance,
       NULL AS from_handle, NULL AS family_handle, NULL AS relation
FROM connection_source AS source
UNION ALL
SELECT 'target', target.handle,
       CASE WHEN reachable.handle IS NULL THEN -1 ELSE 0 END,
       NULL, NULL, NULL
FROM connection_target AS target
LEFT JOIN connection_reachable AS reachable ON reachable.handle = target.handle
UNION ALL
SELECT 'edge', edge.to_handle, NULL, edge.from_handle,
       edge.family_handle, edge.relation
FROM relationship_connection_edges AS edge
JOIN connection_reachable AS origin ON origin.handle = edge.from_handle
JOIN connection_reachable AS destination ON destination.handle = edge.to_handle
ORDER BY kind, distance, family_handle, from_handle, handle
""".strip()
    return (
        sql,
        params + [source, source, target, target],
    )


def compile_primary_ancestors_query(
    person: str, *, dialect: Dialect, treeid: Optional[int], include_private: bool
) -> tuple[str, list[Any]]:
    """Walk indexed primary families only; UNION deduplicates ancestors and cycles."""
    root_where, root_params = _table_condition("root", treeid, include_private)
    child_where, child_params = _table_condition("child", treeid, include_private)
    family_where, family_params = _table_condition("family", treeid, include_private)
    parent_where, parent_params = _table_condition("parent", treeid, include_private)
    if dialect == Dialect.SQLITE:
        primary = "json_extract(child.json_data, '$.parent_family_list[0]')"
        visible_ref = (
            ""
            if include_private
            else """
            AND EXISTS (SELECT 1 FROM json_each(family.json_data, '$.child_ref_list') AS ref
                WHERE json_extract(ref.value, '$.ref') = child.handle
                AND COALESCE(json_extract(ref.value, '$.private'), 0) = 0)
        """
        )
    elif dialect == Dialect.POSTGRESQL:
        primary = "child.json_data::jsonb #>> '{parent_family_list,0}'"
        visible_ref = (
            ""
            if include_private
            else """
            AND EXISTS (SELECT 1 FROM jsonb_array_elements(
                COALESCE(family.json_data::jsonb -> 'child_ref_list', '[]'::jsonb)) AS ref(value)
                WHERE ref.value ->> 'ref' = child.handle
                AND NOT COALESCE((ref.value ->> 'private')::boolean, false))
        """
        )
    else:
        raise QueryError(f"ancestor scopes do not support dialect {dialect!r}")
    sql = f"""
WITH RECURSIVE
parent_slots(slot) AS (VALUES (0), (1)),
root_person AS (
    SELECT root.handle FROM person AS root
    WHERE (root.handle = ? OR root.gramps_id = ?) AND {root_where}
),
ancestors(handle) AS (
    SELECT handle FROM root_person
    UNION
    SELECT parent.handle
    FROM ancestors
    JOIN person AS child ON child.handle = ancestors.handle
    JOIN family ON family.handle = {primary}
    CROSS JOIN parent_slots
    JOIN person AS parent ON parent.handle =
        CASE parent_slots.slot WHEN 0 THEN family.father_handle ELSE family.mother_handle END
    WHERE {child_where} AND {family_where} AND {parent_where} {visible_ref}
)
SELECT handle FROM ancestors
WHERE handle NOT IN (SELECT handle FROM root_person)
ORDER BY handle
"""
    return (
        sql,
        [person, person] + root_params + child_params + family_params + parent_params,
    )
