"""Daily ranking and primary-family traversal, including privacy and cycles."""

from datetime import date, timedelta
import json

from gramps_object_query_language.query import Dialect
from gramps_webapi.api.resources.relationship_scope import (
    compile_primary_ancestors_query,
)
from gramps_webapi.api.resources.views import daily_ancestor
from .test_relationship_scope import _database


def test_primary_ancestors_both_sides_cycles_and_privacy():
    db = _database()
    # A's primary parents are B and C; secondary parents must not enter the pool.
    # B and C share a parent E (pedigree collapse). E points back to A (bad cycle).
    for person, families in [
        ("A", ["parents", "secondary"]),
        ("B", ["grandparents"]),
        ("C", ["grandparents"]),
        ("E", ["cycle"]),
    ]:
        db.execute(
            "UPDATE person SET json_data = ? WHERE handle = ?",
            (json.dumps({"parent_family_list": families}), person),
        )
    for handle, father, mother, children in [
        ("secondary", "D", None, ["A"]),
        ("grandparents", "E", "P", ["B", "C"]),
        ("cycle", "A", None, ["E"]),
    ]:
        db.execute(
            "INSERT INTO family VALUES (?, ?, ?, ?, 0)",
            (
                handle,
                father,
                mother,
                json.dumps({"child_ref_list": [{"ref": child} for child in children]}),
            ),
        )

    def ancestors(include_private=False):
        sql, params = compile_primary_ancestors_query(
            "A", dialect=Dialect.SQLITE, treeid=None, include_private=include_private
        )
        return [row[0] for row in db.execute(sql, params)]

    assert ancestors() == ["B", "C", "E"]
    assert ancestors(True) == ["B", "C", "E", "P"]
    db.execute("UPDATE family SET private = 1 WHERE handle = 'grandparents'")
    assert ancestors() == ["B", "C"]
    # Hidden child references also block traversal.
    db.execute(
        "UPDATE family SET json_data = ? WHERE handle = 'parents'",
        (json.dumps({"child_ref_list": [{"ref": "A", "private": True}]}),),
    )
    assert ancestors() == []


def test_deterministic_daily_selection():
    day = date(2026, 9, 23)
    handles = ["A", "B", "C", "D"]
    chosen = daily_ancestor(handles, day, "home")
    assert chosen == daily_ancestor(list(reversed(handles)), day, "home")
    assert chosen in handles
    assert (
        len(
            {
                daily_ancestor(handles, day + timedelta(days=n), "home")
                for n in range(30)
            }
        )
        == 4
    )
    assert daily_ancestor([], day, "home") is None
    assert daily_ancestor(["A"], day, "home") == "A"
