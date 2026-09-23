"""Portraits in compact search and picker summaries."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from gramps_webapi.api.resources import views


@pytest.fixture
def summaries_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
    CREATE TABLE person (handle TEXT, gramps_id TEXT, gender INTEGER, change INTEGER,
      private INTEGER, birth_ref_index INTEGER, treeid INTEGER, json_data TEXT);
    CREATE TABLE event (handle TEXT, place TEXT, private INTEGER, treeid INTEGER, json_data TEXT);
    CREATE TABLE place (handle TEXT, private INTEGER, treeid INTEGER, json_data TEXT);
    CREATE TABLE media (handle TEXT, private INTEGER, treeid INTEGER, json_data TEXT);
    """)
    monkeypatch.setattr(
        views, "_resolve_dialect", lambda db: SimpleNamespace(value="sqlite")
    )
    monkeypatch.setattr(views, "_resolve_treeid", lambda db: 1)
    monkeypatch.setattr(views, "has_permissions", lambda permissions: False)
    yield conn, SimpleNamespace(dbapi=conn.cursor())
    conn.close()


def add_person(conn, refs):
    person = {
        "primary_name": {
            "first_name": "Marie",
            "surname_list": [{"surname": "Merck", "primary": True}],
        },
        "event_ref_list": [],
        "media_list": refs,
    }
    conn.execute(
        "INSERT INTO person VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("person", "I1", 0, 1, 0, -1, 1, json.dumps(person)),
    )


def add_media(conn, private=False, treeid=1):
    conn.execute(
        "INSERT INTO media VALUES (?, ?, ?, ?)",
        (
            "photo",
            int(private),
            treeid,
            json.dumps({"handle": "photo", "checksum": "current-checksum"}),
        ),
    )


def test_search_summary_includes_primary_portrait_crop_and_checksum(summaries_db):
    conn, db = summaries_db
    ref = {"ref": "photo", "rect": [12, 21, 43, 61]}
    add_person(conn, [ref, {"ref": "secondary"}])
    add_media(conn)
    person = views.get_person_card_summaries(db, ["person"], None)["person"]
    assert person["media_list"] == [ref]
    assert person["extended"]["media"] == [
        {"handle": "photo", "checksum": "current-checksum"}
    ]
    assert "event_ref_list" not in person


@pytest.mark.parametrize(
    "case", ["private_ref", "private_media", "other_tree", "missing", "no_photo"]
)
def test_search_summary_does_not_expose_unavailable_portraits(summaries_db, case):
    conn, db = summaries_db
    refs = (
        []
        if case == "no_photo"
        else [{"ref": "photo", "rect": [], "private": case == "private_ref"}]
    )
    add_person(conn, refs)
    if case != "missing":
        add_media(
            conn,
            private=case == "private_media",
            treeid=2 if case == "other_tree" else 1,
        )
    person = views.get_person_card_summaries(db, ["person"], None)["person"]
    assert person["media_list"] == []


def test_authorized_summary_keeps_private_primary_portrait(summaries_db, monkeypatch):
    conn, db = summaries_db
    monkeypatch.setattr(views, "has_permissions", lambda permissions: True)
    add_person(conn, [{"ref": "photo", "rect": [], "private": True}])
    add_media(conn, private=True)
    person = views.get_person_card_summaries(db, ["person"], None)["person"]
    assert person["media_list"] == [{"ref": "photo", "rect": []}]


def test_authorized_search_keeps_private_primary_name(summaries_db, monkeypatch):
    conn, db = summaries_db
    monkeypatch.setattr(views, "has_permissions", lambda permissions: True)
    add_person(conn, [])
    conn.execute(
        "UPDATE person SET json_data=json_set(json_data, '$.primary_name.private', json('true'))"
    )
    person = views.get_person_card_summaries(db, ["person"], None)["person"]
    assert person["profile"]["name_given"] == "Marie"
    assert person["profile"]["name_surname"] == "Merck"


@pytest.mark.parametrize("include_private", [False, True])
def test_graph_private_names_follow_viewer_permissions(include_private):
    name = {
        "first_name": "Ursula Maria",
        "private": True,
        "surname_list": [{"surname": "Naprawnik", "primary": True}],
    }
    row = ("person", "ursula", "I1", 0, name, [name], None, [], None, None, None)
    person = views._person_graph_projection(row, None, include_private=include_private)
    assert person["profile"]["name_display"] == (
        "Ursula Maria Naprawnik" if include_private else ""
    )
    assert len(person["alternate_names"]) == int(include_private)


def test_unauthorized_search_hides_private_name(summaries_db):
    conn, db = summaries_db
    add_person(conn, [])
    conn.execute(
        "UPDATE person SET json_data=json_set(json_data, '$.primary_name.private', json('true'))"
    )
    person = views.get_person_card_summaries(db, ["person"], None)["person"]
    assert person["primary_name"] == {}
    assert person["profile"]["name_given"] == ""
    assert person["profile"]["name_surname"] == ""
