"""Shortest paths through the person-family graph."""

from collections import deque


def _family_members(family) -> tuple[list[str], set[str], set[str]]:
    """Return all people, parents, and children referenced by a family."""
    parent_handles = [family.get_father_handle(), family.get_mother_handle()]
    child_handles = [ref.ref for ref in family.get_child_ref_list()]
    members = list(
        dict.fromkeys(handle for handle in parent_handles + child_handles if handle)
    )
    return members, set(parent_handles) - {None, ""}, set(child_handles) - {None, ""}


def _step_relation(
    from_handle: str, to_handle: str, parents: set[str], children: set[str]
) -> str:
    """Describe the destination relative to the source within one family."""
    if from_handle in parents and to_handle in parents:
        return "partner"
    if from_handle in parents and to_handle in children:
        return "child"
    if from_handle in children and to_handle in parents:
        return "parent"
    if from_handle in children and to_handle in children:
        return "sibling"
    raise ValueError("Family path step contains people outside the family")


def _type_name(value) -> str:
    """Return the stable, untranslated XML name of a Gramps type."""
    return value.xml_str() if hasattr(value, "xml_str") else str(value)


def _child_ref(family, child_handle: str):
    return next(
        ref for ref in family.get_child_ref_list() if ref.ref == child_handle
    )


def _parent_child_relationship_type(
    family, child_handle: str, parent_handle: str
) -> str:
    """Return the relationship of one child to the specific traversed parent."""
    child_ref = _child_ref(family, child_handle)
    if parent_handle == family.get_father_handle():
        return _type_name(child_ref.frel)
    if parent_handle == family.get_mother_handle():
        return _type_name(child_ref.mrel)
    raise ValueError("Parent-child path step does not reference a family parent")


def _sibling_relationship_type(family, from_handle: str, to_handle: str) -> str:
    """Describe the strongest shared-parent route between two children."""
    from_ref = _child_ref(family, from_handle)
    to_ref = _child_ref(family, to_handle)
    candidates = []
    if family.get_father_handle():
        candidates.append((_type_name(from_ref.frel), _type_name(to_ref.frel)))
    if family.get_mother_handle():
        candidates.append((_type_name(from_ref.mrel), _type_name(to_ref.mrel)))

    rank = {
        "Birth": 0,
        "Adopted": 1,
        "Stepchild": 2,
        "Foster": 3,
        "Sponsored": 4,
        "Other": 5,
        "Custom": 5,
        "None": 6,
        "Unknown": 6,
    }

    def candidate_type(pair: tuple[str, str]) -> str:
        return max(pair, key=lambda item: rank.get(item, rank["Other"]))

    return min(
        (candidate_type(pair) for pair in candidates),
        key=lambda item: rank.get(item, rank["Other"]),
        default="Unknown",
    )


def _step_relationship_type(
    family, from_handle: str, to_handle: str, relation: str
) -> str:
    if relation == "partner":
        return _type_name(family.get_relationship())
    if relation == "child":
        return _parent_child_relationship_type(family, to_handle, from_handle)
    if relation == "parent":
        return _parent_child_relationship_type(family, from_handle, to_handle)
    if relation == "sibling":
        return _sibling_relationship_type(family, from_handle, to_handle)
    raise ValueError(f"Unsupported path relation: {relation}")


def find_connection_path(db_handle, handle1: str, handle2: str) -> dict:
    """Find a shortest path between people through all of their families.

    Unlike Gramps' relationship calculator, this graph traversal also follows
    partners and therefore finds step-family and in-law connections. There is
    deliberately no generation limit: the finite database is the boundary.
    """
    if handle1 == handle2:
        return {
            "connected": True,
            "person_handles": [handle1],
            "family_handles": [],
            "steps": [],
        }

    pending = deque([handle1])
    previous: dict[str, tuple[str, str, str, str]] = {}
    visited = {handle1}
    visited_families = set()

    while pending:
        current_handle = pending.popleft()
        person = db_handle.get_person_from_handle(current_handle)
        family_handles = dict.fromkeys(
            person.get_family_handle_list() + person.get_parent_family_handle_list()
        )
        for family_handle in family_handles:
            if family_handle in visited_families:
                continue
            visited_families.add(family_handle)
            family = db_handle.get_family_from_handle(family_handle)
            members, parents, children = _family_members(family)
            for neighbour_handle in members:
                if neighbour_handle == current_handle or neighbour_handle in visited:
                    continue
                relation = _step_relation(
                    current_handle, neighbour_handle, parents, children
                )
                relationship_type = _step_relationship_type(
                    family, current_handle, neighbour_handle, relation
                )
                visited.add(neighbour_handle)
                previous[neighbour_handle] = (
                    current_handle,
                    family_handle,
                    relation,
                    relationship_type,
                )
                if neighbour_handle == handle2:
                    pending.clear()
                    break
                pending.append(neighbour_handle)
            if handle2 in previous:
                break

    if handle2 not in previous:
        return {
            "connected": False,
            "person_handles": [],
            "family_handles": [],
            "steps": [],
        }

    steps = []
    current_handle = handle2
    while current_handle != handle1:
        from_handle, family_handle, relation, relationship_type = previous[
            current_handle
        ]
        steps.append(
            {
                "from_handle": from_handle,
                "to_handle": current_handle,
                "family_handle": family_handle,
                "relation": relation,
                "relationship_type": relationship_type,
            }
        )
        current_handle = from_handle
    steps.reverse()
    return {
        "connected": True,
        "person_handles": [handle1] + [step["to_handle"] for step in steps],
        "family_handles": [step["family_handle"] for step in steps],
        "steps": steps,
    }
