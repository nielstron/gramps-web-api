from unittest.mock import MagicMock, patch

from gramps.gen.lib import Source, Tag
from gramps_webapi.api.resources.util import add_object, update_object
from gramps_webapi.api.resources.sort import sort_objects


def source():
    obj = Source()
    obj.handle = "post"
    obj.gramps_id = "S1"
    obj.tag_list = ["blog"]
    return obj


def test_publication_stamped_when_source_is_first_published():
    db = MagicMock(readonly=False)
    tag = Tag()
    tag.set_name("Blog")
    db.get_tag_from_handle.return_value = tag
    post = source()
    add_object(db, post, MagicMock())
    dates = [
        a.value for a in post.attribute_list if str(a.type) == "Blog publication date"
    ]
    assert len(dates) == 1
    old_date = dates[0]
    db.method.return_value = MagicMock(return_value=post)
    with patch("gramps_webapi.api.resources.util.has_handle", return_value=True):
        update_object(db, post, MagicMock())
    assert [
        a.value for a in post.attribute_list if str(a.type) == "Blog publication date"
    ] == [old_date]


def test_draft_has_no_publication_date():
    db = MagicMock(readonly=False)
    post = source()
    post.private = True
    add_object(db, post, MagicMock())
    assert not post.attribute_list


def test_unpublishing_and_republishing_preserve_first_publication():
    from copy import deepcopy
    from gramps_webapi.blog import ensure_publication_date, publication_date

    db = MagicMock()
    tag = Tag()
    tag.set_name("Blog")
    db.get_tag_from_handle.return_value = tag
    original = source()
    ensure_publication_date(db, original)
    timestamp = publication_date(original)
    edited = deepcopy(original)
    edited.private = True
    edited.attribute_list = []
    ensure_publication_date(db, edited, original)
    assert publication_date(edited) == timestamp
    published = deepcopy(edited)
    published.private = False
    ensure_publication_date(db, published, edited)
    assert publication_date(published) == timestamp


def test_publication_sort_ignores_last_change():
    from gramps.gen.lib import SrcAttribute

    posts = []
    for published, change in [
        ("2026-09-10T12:00:00Z", 999),
        ("2026-09-11T12:00:00Z", 1),
    ]:
        post = source()
        post.change = change
        attr = SrcAttribute()
        attr.set_type("Blog publication date")
        attr.set_value(published)
        post.attribute_list = [attr]
        posts.append(post)
    expected = list(reversed(posts))
    assert sort_objects(MagicMock(), "Source", posts, ["-publication"]) == expected
