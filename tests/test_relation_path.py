"""Tests for arbitrary connection paths through families."""

import gettext
import locale
import unittest

# Standalone python.org/uv builds on macOS omit these gettext wrappers even
# though Gramps imports them from locale. CPython builds used in production
# already provide them.
if not hasattr(locale, "textdomain"):
    locale.textdomain = gettext.textdomain
    locale.bindtextdomain = gettext.bindtextdomain

from gramps_webapi.api.relation_path import find_connection_path


class ChildRef:
    def __init__(self, ref, frel="Birth", mrel="Birth"):
        self.ref = ref
        self.frel = frel
        self.mrel = mrel


class Person:
    def __init__(self, families=(), parent_families=()):
        self.families = list(families)
        self.parent_families = list(parent_families)

    def get_family_handle_list(self):
        return self.families

    def get_parent_family_handle_list(self):
        return self.parent_families


class Family:
    def __init__(self, father, mother, children=(), relationship="Married"):
        self.father = father
        self.mother = mother
        self.children = list(children)
        self.relationship = relationship

    def get_father_handle(self):
        return self.father

    def get_mother_handle(self):
        return self.mother

    def get_child_ref_list(self):
        return [
            child if isinstance(child, ChildRef) else ChildRef(child)
            for child in self.children
        ]

    def get_relationship(self):
        return self.relationship


class Database:
    def __init__(self):
        self.people = {
            "A": Person(["F1"]),
            "B": Person(["F1"]),
            "C": Person(parent_families=["F1"]),
            "D": Person(["F2"], ["F1"]),
            "E": Person(["F2"]),
            "X": Person(),
        }
        self.families = {
            "F1": Family(
                "A",
                "B",
                [ChildRef("C", "Birth", "Unknown"), ChildRef("D")],
            ),
            "F2": Family("D", "E", relationship="Unknown"),
        }

    def get_person_from_handle(self, handle):
        return self.people[handle]

    def get_family_from_handle(self, handle):
        return self.families[handle]


class TestRelationPath(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_traverses_sibling_and_partner_for_non_blood_connection(self):
        result = find_connection_path(self.db, "C", "E")

        self.assertEqual(result["person_handles"], ["C", "D", "E"])
        self.assertEqual(
            [step["relation"] for step in result["steps"]],
            ["sibling", "partner"],
        )

    def test_labels_direction_of_parent_child_step(self):
        father_to_child = find_connection_path(self.db, "A", "C")["steps"][0]
        child_to_father = find_connection_path(self.db, "C", "A")["steps"][0]
        mother_to_child = find_connection_path(self.db, "B", "C")["steps"][0]

        self.assertEqual(father_to_child["relation"], "child")
        self.assertEqual(father_to_child["relationship_type"], "Birth")
        self.assertEqual(child_to_father["relation"], "parent")
        self.assertEqual(child_to_father["relationship_type"], "Birth")
        self.assertEqual(mother_to_child["relationship_type"], "Unknown")

    def test_exposes_unknown_partner_relationship(self):
        step = find_connection_path(self.db, "D", "E")["steps"][0]

        self.assertEqual(step["relation"], "partner")
        self.assertEqual(step["relationship_type"], "Unknown")

    def test_returns_disconnected_result(self):
        self.assertEqual(
            find_connection_path(self.db, "A", "X"),
            {
                "connected": False,
                "person_handles": [],
                "family_handles": [],
                "steps": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
