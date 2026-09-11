"""Name matching must be useful without guessing between family members."""

import pytest
from gramps.gen.lib import Name, Person, Surname

from gramps_webapi.api.home_person import find_home_person


def make_person(gramps_id, given, surname, private=False):
    person = Person()
    person.gramps_id = gramps_id
    person.set_privacy(private)
    name = Name()
    name.set_first_name(given)
    family_name = Surname()
    family_name.set_surname(surname)
    name.add_surname(family_name)
    person.set_primary_name(name)
    return person


@pytest.mark.parametrize(
    "full_name,given,surname",
    [
        ("MUNDLER, Niels", "Niels", "Mündler"),
        ("Nils Muendler", "Niels", "Mündler"),
        ("Niels Mündler", "Niels Adrian", "Mündler"),
        ("Niels Mündler", "Niels Torsten Jens Friedrich", "Mündler-Sasahara"),
        ("Anne Marie Smith", "Anne-Marie", "Smith"),
    ],
)
def test_close_names(full_name, given, surname):
    people = [
        make_person("I1", given, surname),
        make_person("I2", "Unrelated", "Person"),
    ]
    assert find_home_person(full_name, people) == "I1"


@pytest.mark.parametrize("name", ["", "Niels", "Other Person", "Tim Mündler"])
def test_insufficient_or_unrelated_names(name):
    assert find_home_person(name, [make_person("I1", "Niels", "Mündler")]) is None


def test_duplicate_and_nearly_equal_matches_are_ambiguous():
    people = [
        make_person("I1", "Niels", "Mündler"),
        make_person("I2", "Niels", "Mündler"),
    ]
    assert find_home_person("Niels Mündler", people) is None
    people[1] = make_person("I2", "Niels Adrian", "Mündler")
    assert find_home_person("Niels Mündler", people) is None


def test_alternate_names_and_private_names():
    person = make_person("I1", "Niels", "Mündler")
    alternate = make_person("unused", "Niels", "Example").get_primary_name()
    person.add_alternate_name(alternate)
    assert find_home_person("Niels Example", [person]) == "I1"
    alternate.set_privacy(True)
    assert find_home_person("Niels Example", [person]) is None
    assert find_home_person("Niels Example", [person], view_private=True) == "I1"
    person.set_privacy(True)
    assert find_home_person("Niels Mündler", [person]) is None
    assert find_home_person("Niels Mündler", [person], view_private=True) == "I1"
