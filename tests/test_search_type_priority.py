"""Tests for ordered object-type groups in full-text search."""

import gettext
import locale

if not hasattr(locale, "textdomain"):
    locale.textdomain = gettext.textdomain
    locale.bindtextdomain = gettext.bindtextdomain

from gramps_webapi.api.search.indexer import SearchIndexerBase


class FakeCollection:
    """Small in-memory stand-in for the search collection."""

    def __init__(self):
        types = ["person", "family", "note", "event", "event", "event", "event"]
        self.documents = [
            {
                "metadata": {"handle": f"handle-{index}", "type": object_type},
                "rank": 1 / (index + 1),
                "content": f"content-{index}",
            }
            for index, object_type in enumerate(types)
        ]

    def query(self, query, *, limit, offset, order_by, where, vector_search):
        del query, order_by, vector_search
        documents = self.documents
        if where and "type" in where:
            allowed_types = where["type"]["$in"]
            documents = [
                document
                for document in documents
                if document["metadata"]["type"] in allowed_types
            ]
        return {
            "total": len(documents),
            "results": documents[offset : offset + limit],
        }


def test_search_respects_requested_object_type_priority_across_pages():
    """Requested object types group results in that order before paging."""
    indexer = object.__new__(SearchIndexerBase)
    indexer.index = FakeCollection()
    indexer.index_public = indexer.index
    indexer.use_semantic_text = False
    object_types = ["event", "person", "family", "note"]

    total, first_page = indexer.search(
        "Lewis von", page=1, pagesize=3, object_types=object_types
    )
    assert total == 7
    assert [hit["object_type"] for hit in first_page] == [
        "event",
        "event",
        "event",
    ]
    assert [hit["rank"] for hit in first_page] == [0, 1, 2]

    total, second_page = indexer.search(
        "Lewis von", page=2, pagesize=3, object_types=object_types
    )
    assert total == 7
    assert [hit["object_type"] for hit in second_page] == [
        "event",
        "person",
        "family",
    ]
    assert [hit["rank"] for hit in second_page] == [3, 4, 5]
