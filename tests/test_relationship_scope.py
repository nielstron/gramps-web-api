"""Tests for recursive SQL relationship scopes."""

import json
import sqlite3

from gramps_object_query_language.query import (
    FAMILY,
    PERSON,
    Dialect,
    Query,
    compile_query,
)

from gramps_webapi.api.resources.relationship_scope import (
    RelationshipScope,
    RelationshipScopePredicate,
    compile_connection_path_query,
    compile_relationship_scope,
    prefix_relationship_scope,
)


def _database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE person (
            handle TEXT PRIMARY KEY, gramps_id TEXT, json_data TEXT,
            private INTEGER
        );
        CREATE TABLE family (
            handle TEXT PRIMARY KEY, father_handle TEXT, mother_handle TEXT,
            json_data TEXT, private INTEGER
        );
        CREATE TABLE event (
            handle TEXT PRIMARY KEY, private INTEGER
        );
        CREATE TABLE reference (
            obj_handle TEXT, obj_class TEXT, ref_handle TEXT, ref_class TEXT
        );
        """)
    db.executemany(
        "INSERT INTO person VALUES (?, ?, ?, ?)",
        [
            (
                handle,
                handle,
                json.dumps(
                    {
                        "event_ref_list": (
                            [
                                {"ref": "person-event", "private": False},
                                {"ref": "private-event", "private": False},
                                {"ref": "hidden-person-event", "private": True},
                            ]
                            if handle == "A"
                            else []
                        )
                    }
                ),
                private,
            )
            for handle, private in [
                ("A", 0),
                ("B", 0),
                ("C", 0),
                ("D", 0),
                ("E", 0),
                ("P", 1),
            ]
        ],
    )

    def family(handle, father, mother, children, private=0):
        return (
            handle,
            father,
            mother,
            json.dumps(
                {
                    "child_ref_list": [{"ref": child} for child in children],
                    "event_ref_list": (
                        [
                            {"ref": "family-event", "private": False},
                            {"ref": "hidden-family-event", "private": True},
                        ]
                        if handle == "couple"
                        else []
                    ),
                }
            ),
            private,
        )

    db.executemany(
        "INSERT INTO family VALUES (?, ?, ?, ?, ?)",
        [
            family("parents", "B", "C", ["A", "D"]),
            family("couple", "A", "E", []),
            family("private-family", "A", "P", [], private=1),
        ],
    )
    db.executemany(
        "INSERT INTO event VALUES (?, ?)",
        [
            ("person-event", 0),
            ("family-event", 0),
            ("private-event", 1),
            ("hidden-person-event", 0),
            ("hidden-family-event", 0),
        ],
    )
    db.executemany(
        "INSERT INTO reference VALUES (?, ?, ?, ?)",
        [
            ("A", "Person", "person-event", "Event"),
            ("couple", "Family", "family-event", "Event"),
            ("A", "Person", "private-event", "Event"),
            ("A", "Person", "hidden-person-event", "Event"),
            ("couple", "Family", "hidden-family-event", "Event"),
        ],
    )
    return db


def _handles(db, scope, relation, include_private=True):
    cte, params = compile_relationship_scope(
        scope,
        dialect=Dialect.SQLITE,
        treeid=None,
        include_private=include_private,
    )
    return {
        row[0]
        for row in db.execute(
            f"{cte} SELECT handle FROM relationship_scope_{relation}", params
        )
    }


def test_relationship_degree_and_shared_relations():
    db = _database()
    assert _handles(db, RelationshipScope("A", 1), "persons") == {
        "A",
        "B",
        "C",
        "E",
        "P",
    }
    assert _handles(db, RelationshipScope("A", 2), "persons") == {
        "A",
        "B",
        "C",
        "D",
        "E",
        "P",
    }
    assert _handles(db, RelationshipScope("A", 0), "families") == {
        "parents",
        "couple",
        "private-family",
    }
    assert _handles(db, RelationshipScope("A", 0), "events") == {
        "person-event",
        "family-event",
        "private-event",
        "hidden-person-event",
        "hidden-family-event",
    }


def test_direction_and_privacy_are_applied_inside_the_recursion():
    db = _database()
    assert _handles(db, RelationshipScope("A", 1, "ancestors"), "persons", False) == {
        "A",
        "B",
        "C",
    }
    assert _handles(db, RelationshipScope("B", 1, "descendants"), "persons", False) == {
        "A",
        "D",
        "B",
    }
    assert "P" not in _handles(db, RelationshipScope("A", 1), "persons", False)
    assert "private-family" not in _handles(
        db, RelationshipScope("A", 0), "families", False
    )
    assert "private-event" not in _handles(
        db, RelationshipScope("A", 0), "events", False
    )
    assert "hidden-person-event" not in _handles(
        db, RelationshipScope("A", 0), "events", False
    )
    assert "hidden-family-event" not in _handles(
        db, RelationshipScope("A", 0), "events", False
    )


def test_postgresql_uses_jsonb_collection_expansion():
    sql, params = compile_relationship_scope(
        RelationshipScope("I1", 4),
        dialect=Dialect.POSTGRESQL,
        treeid=7,
        include_private=False,
    )
    assert "jsonb_array_elements" in sql
    assert "relationship_scope_events" in sql
    assert params.count(7) == 6


def test_scope_wraps_an_ordinary_structured_query():
    db = _database()
    sql, params = compile_query(
        PERSON,
        Query(select=["handle"], where=RelationshipScopePredicate(), limit=20),
        dialect=Dialect.SQLITE,
    )
    sql, params = prefix_relationship_scope(
        sql,
        params,
        RelationshipScope("A", 1),
        dialect=Dialect.SQLITE,
        treeid=None,
        include_private=False,
    )
    assert {row[0] for row in db.execute(sql, params)} == {"A", "B", "C", "E"}


def test_scope_uses_the_irregular_family_relation_plural():
    sql, _ = compile_query(
        FAMILY,
        Query(select=["handle"], where=RelationshipScopePredicate(), limit=20),
        dialect=Dialect.SQLITE,
    )
    assert "relationship_scope_families" in sql
    assert "relationship_scope_familys" not in sql


def test_connection_query_returns_shortest_parent_and_sibling_edges():
    db = _database()
    sql, params = compile_connection_path_query(
        "A",
        "D",
        dialect=Dialect.SQLITE,
        treeid=None,
        include_private=False,
    )
    rows = db.execute(sql, params).fetchall()
    target = next(row for row in rows if row[0] == "target")
    assert target[1:3] == ("D", 0)
    sibling_edges = [row for row in rows if row[0] == "edge" and row[5] == "sibling"]
    assert ("edge", "D", None, "A", "parents", "sibling") in sibling_edges


def test_connection_query_distinguishes_a_missing_target():
    db = _database()
    sql, params = compile_connection_path_query(
        "A",
        "missing",
        dialect=Dialect.SQLITE,
        treeid=None,
        include_private=False,
    )
    rows = db.execute(sql, params).fetchall()
    assert any(row[0] == "source" for row in rows)
    assert not any(row[0] == "target" for row in rows)
