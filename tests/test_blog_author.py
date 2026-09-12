"""Author resolution must respect tree and person visibility."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask
from gramps.gen.lib import Source, SrcAttribute

from gramps_webapi.api.resources import sources


@pytest.mark.parametrize("visible", [True, False])
def test_author_uses_current_account_and_visible_home_person(monkeypatch, visible):
    source = Source()
    source.set_author("Old name")
    attribute = SrcAttribute()
    attribute.set_type("Blog author")
    attribute.set_value("239b684f-d415-4d4f-849a-428dc0b76e8c")
    source.add_attribute(attribute)
    query = Mock()
    query.filter_by.return_value.first.return_value = SimpleNamespace(
        fullname="Current name", name="writer", settings={"homePerson": "I42"}
    )
    monkeypatch.setattr(sources, "User", SimpleNamespace(query=query))
    monkeypatch.setattr(sources, "get_tree_from_jwt_or_fail", lambda: "tree-a")
    resource = sources.SourceAuthorResource()
    monkeypatch.setattr(resource, "get_object_from_handle", lambda handle: source)
    db = Mock()
    db.get_person_from_gramps_id.return_value = (
        SimpleNamespace(gramps_id="I42") if visible else None
    )
    monkeypatch.setattr(
        sources.SourceResourceHelper, "db_handle", property(lambda self: db)
    )
    with Flask(__name__).app_context():
        result = resource.get("source").get_json()
    query.filter_by.assert_called_once_with(id=attribute.get_value(), tree="tree-a")
    assert result == {
        "name": "Current name",
        "username": "writer",
        "person_id": "I42" if visible else None,
    }


def test_missing_or_other_tree_author_retains_legacy_text(monkeypatch):
    source = Source()
    source.set_author("Historical author")
    attribute = SrcAttribute()
    attribute.set_type("Blog author")
    attribute.set_value("239b684f-d415-4d4f-849a-428dc0b76e8c")
    source.add_attribute(attribute)
    query = Mock()
    query.filter_by.return_value.first.return_value = None
    monkeypatch.setattr(sources, "User", SimpleNamespace(query=query))
    monkeypatch.setattr(sources, "get_tree_from_jwt_or_fail", lambda: "tree-a")
    resource = sources.SourceAuthorResource()
    monkeypatch.setattr(resource, "get_object_from_handle", lambda handle: source)
    with Flask(__name__).app_context():
        assert resource.get("source").get_json() == {
            "name": "Historical author",
            "username": None,
            "person_id": None,
        }
