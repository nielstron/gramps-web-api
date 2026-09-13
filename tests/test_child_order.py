"""Child chronology must be derived, never depend on insertion order."""

from unittest.mock import MagicMock, patch
from copy import deepcopy

import pytest
from gramps.gen.lib import ChildRef, Date, Event, EventRef, Family, Person

from gramps_webapi.api.resources.util import get_family_by_handle, update_object
from gramps_webapi.api.resources.families import FamilyResourceHelper


@pytest.fixture
def family_db():
    db = MagicMock()
    db.readonly = False
    family = Family()
    family.handle = "family"
    people = {}
    events = {}
    for handle, year in [("younger", 2005), ("unknown", 0), ("older", 2000)]:
        person = Person()
        person.handle = handle
        person.set_parent_family_handle_list([family.handle])
        event = Event()
        event.handle = handle + "-birth"
        event.set_type("Birth")
        date = Date()
        if year:
            date.set(value=(0, 0, year, False))
        event.set_date_object(date)
        ref = EventRef()
        ref.ref = event.handle
        person.add_event_ref(ref)
        person.set_birth_ref(ref)
        people[handle] = person
        events[event.handle] = event
        child = ChildRef()
        child.ref = handle
        family.add_child_ref(child)
    db.get_family_from_handle.side_effect = lambda h: deepcopy(family)
    db.get_person_from_handle.side_effect = people.__getitem__
    db.get_event_from_handle.side_effect = events.__getitem__
    return db, family, people, events


def test_family_read_is_chronological(family_db):
    db, family, _, _ = family_db
    result = get_family_by_handle(db, family.handle)
    assert [ref.ref for ref in result.child_ref_list] == ["older", "younger", "unknown"]


def test_family_endpoint_sorts_before_profiles(family_db):
    db, family, _, _ = family_db
    result = FamilyResourceHelper.object_extend(MagicMock(db_handle=db), family, {})
    assert [ref.ref for ref in result.child_ref_list] == ["older", "younger", "unknown"]


def test_changed_birth_date_immediately_changes_read_order(family_db):
    db, family, _, events = family_db
    events["younger-birth"].date.set(value=(0, 0, 1999, False))
    result = get_family_by_handle(db, family.handle)
    assert [ref.ref for ref in result.child_ref_list] == ["younger", "older", "unknown"]


def test_family_write_discards_custom_order(family_db):
    db, family, _, _ = family_db
    family.gramps_id = "F1"
    commit = MagicMock()
    db.method.side_effect = lambda method, kind: (
        db.get_family_from_handle if method.startswith("get_") else commit
    )
    with (
        patch("gramps_webapi.api.resources.util.has_handle", return_value=True),
        patch("gramps_webapi.api.resources.util.update_family_update_refs"),
    ):
        update_object(db, family, MagicMock())
    assert [r.ref for r in commit.call_args.args[0].child_ref_list] == [
        "older",
        "younger",
        "unknown",
    ]
