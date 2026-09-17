"""Tests for SQL-backed frontend view resources."""

import unittest

from gramps_webapi.api.resources.views import RelationshipGraphArgs

from . import BASE_URL, get_test_client
from .checks import check_requires_token, check_success

VIEWS_URL = BASE_URL + "/views/"
PERSON1 = "cc8205d87831c772e87"
PERSON2 = "cc8205d872f532ab14e"


class TestRelationshipGraphView(unittest.TestCase):
    """The relationship graph is delivered as one compact response."""

    @classmethod
    def setUpClass(cls):
        cls.client = get_test_client()

    def test_requires_token(self):
        check_requires_token(self, f"{VIEWS_URL}relationship-graph/{PERSON1}")

    def test_defaults_to_ten_degrees(self):
        self.assertEqual(RelationshipGraphArgs().load({})["degree"], 10)

    def test_returns_people_with_graph_projection(self):
        result = check_success(
            self, f"{VIEWS_URL}relationship-graph/{PERSON1}?degree=1"
        )
        self.assertIn(PERSON1, {person["handle"] for person in result["people"]})
        person = next(item for item in result["people"] if item["handle"] == PERSON1)
        self.assertIn("profile", person)
        self.assertIn("families", result)
        self.assertIn("family_handles", person)
        self.assertNotIn("event_ref_list", person)
        self.assertEqual(
            set(person),
            {
                "handle",
                "gramps_id",
                "primary_name",
                "alternate_names",
                "media_list",
                "profile",
                "family_handles",
                "primary_parent_family_handle",
            },
        )


class TestConnectionGraphView(unittest.TestCase):
    """A connection graph combines path, people, and families."""

    @classmethod
    def setUpClass(cls):
        cls.client = get_test_client()

    def test_returns_complete_partner_graph(self):
        result = check_success(self, f"{VIEWS_URL}connection-graph/{PERSON1}/{PERSON2}")
        self.assertTrue(result["path"]["connected"])
        self.assertEqual(result["path"]["person_handles"], [PERSON1, PERSON2])
        self.assertEqual(
            {person["handle"] for person in result["people"]}, {PERSON1, PERSON2}
        )
        self.assertEqual(len(result["families"]), 1)


class TestAnniversariesView(unittest.TestCase):
    """Anniversary filtering and relationship distance happen server-side."""

    @classmethod
    def setUpClass(cls):
        cls.client = get_test_client()

    def test_returns_bounded_event_list(self):
        result = check_success(
            self,
            f"{VIEWS_URL}anniversaries/{PERSON1}?month=1&day=1&degree=4&limit=10",
        )
        self.assertIn("events", result)
        self.assertLessEqual(len(result["events"]), 10)


class TestRecentChangesView(unittest.TestCase):
    """The dashboard change feed is a compact direct database view."""

    @classmethod
    def setUpClass(cls):
        cls.client = get_test_client()

    def test_requires_token(self):
        check_requires_token(self, f"{VIEWS_URL}recent-changes")

    def test_returns_sorted_compact_objects(self):
        result = check_success(self, f"{VIEWS_URL}recent-changes?limit=8")
        self.assertEqual(len(result), 8)
        changes = [item["object"]["change"] for item in result]
        self.assertEqual(changes, sorted(changes, reverse=True))
        for item in result:
            self.assertEqual(item["handle"], item["object"]["handle"])
            self.assertIn("gramps_id", item["object"])
            self.assertLessEqual(len(item["object"]), 8)

    def test_since_can_exclude_all_objects(self):
        result = check_success(
            self, f"{VIEWS_URL}recent-changes?since=9999999999&limit=8"
        )
        self.assertEqual(result, [])

    def test_can_filter_to_picker_object_types(self):
        result = check_success(
            self, f"{VIEWS_URL}recent-changes?limit=8&type=person,event"
        )
        self.assertTrue(result)
        self.assertLessEqual(
            {item["object_type"] for item in result}, {"person", "event"}
        )


class TestObjectSummariesView(unittest.TestCase):
    """Picker history and bookmarks resolve in one compact request."""

    @classmethod
    def setUpClass(cls):
        cls.client = get_test_client()

    def test_requires_token(self):
        check_requires_token(
            self, f"{VIEWS_URL}object-summaries?objects=person:{PERSON1}"
        )

    def test_resolves_handles_and_gramps_ids_in_request_order(self):
        by_handle = check_success(
            self, f"{VIEWS_URL}object-summaries?objects=person:{PERSON1}&locale=en"
        )
        self.assertEqual(len(by_handle), 1)
        person = by_handle[0]
        self.assertEqual(person["handle"], PERSON1)
        self.assertEqual(person["object_type"], "person")
        self.assertIn("profile", person["object"])
        self.assertNotIn("event_ref_list", person["object"])

        by_id = check_success(
            self,
            f"{VIEWS_URL}object-summaries?objects=person:{person['object']['gramps_id']},person:missing&locale=en",
        )
        self.assertEqual([item["handle"] for item in by_id], [PERSON1])
