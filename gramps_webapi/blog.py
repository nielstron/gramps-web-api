"""Persistent first-publication metadata for sources used as blog posts."""

from datetime import datetime, timezone
from copy import deepcopy

from gramps.gen.lib import SrcAttribute

PUBLICATION_ATTRIBUTE = "Blog publication date"


def publication_date(source):
    return next(
        (
            a.value
            for a in source.attribute_list
            if str(a.type) == PUBLICATION_ATTRIBUTE
        ),
        "",
    )


def ensure_publication_date(db, source, old_source=None):
    """Keep first publication fixed through edits, unpublishing and republishing."""
    old = publication_date(old_source) if old_source else ""
    if old:
        source.attribute_list = [
            a for a in source.attribute_list if str(a.type) != PUBLICATION_ATTRIBUTE
        ]
        attr = next(
            a for a in old_source.attribute_list if str(a.type) == PUBLICATION_ATTRIBUTE
        )
        source.attribute_list.append(deepcopy(attr))
        return
    if publication_date(source) or source.private:
        return
    if not any(db.get_tag_from_handle(h).get_name() == "Blog" for h in source.tag_list):
        return
    attr = SrcAttribute()
    attr.set_type(PUBLICATION_ATTRIBUTE)
    attr.set_value(
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    source.attribute_list.append(attr)
