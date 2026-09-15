"""Custom event type suggestions reflect the current records."""

import gettext
import locale
from unittest.mock import Mock

if not hasattr(locale, "textdomain"):
    locale.textdomain = gettext.textdomain
    locale.bindtextdomain = gettext.bindtextdomain

from gramps.gen.lib import Event, EventType
from gramps_webapi.api.resources.types import get_custom_types


def test_event_types_exclude_deleted_or_retyped_names():
    db = Mock()
    db.get_event_types.return_value = ["Old name", "Travel"]
    travel = Event()
    travel.set_type("Travel")
    birth = Event()
    birth.set_type(EventType.BIRTH)
    db.iter_events.return_value = iter([travel, birth, travel])
    assert get_custom_types(db, "event_types") == ["Travel"]


def test_empty_tree_has_no_custom_event_types():
    db = Mock()
    db.get_event_types.return_value = ["Deleted"]
    db.iter_events.return_value = iter([])
    assert get_custom_types(db, "event_types") == []
