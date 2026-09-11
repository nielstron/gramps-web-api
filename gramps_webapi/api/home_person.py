"""Conservative matching of account names to visible family-tree people."""

import re
from difflib import SequenceMatcher

from unidecode import unidecode


def _tokens(name):
    return re.findall(r"[a-z0-9]+", unidecode(name).lower())


def _similarity(left, right):
    if min(len(left), len(right)) < 2:
        return 0
    shorter, longer = sorted((left, right), key=len)
    remaining = list(longer)
    scores = []
    for token in sorted(shorter, key=len, reverse=True):
        score, index = max(
            (SequenceMatcher(None, token, other).ratio(), index)
            for index, other in enumerate(remaining)
        )
        if score < 0.8:
            return 0
        scores.append(score)
        remaining.pop(index)
    # Missing middle names are common, but a complete match ranks higher.
    return sum(scores) / len(scores) - min(0.04, 0.02 * (len(longer) - len(shorter)))


def find_home_person(full_name, people, view_private=False):
    """Return a Gramps ID only for a close match with a clear lead."""
    query = _tokens(full_name)
    if len(query) < 2:
        return None
    candidates = []
    for person in people:
        if person.get_privacy() and not view_private:
            continue
        names = [person.get_primary_name(), *person.get_alternate_names()]
        score = max(
            (
                _similarity(query, _tokens(name.get_regular_name()))
                for name in names
                if view_private or not name.get_privacy()
            ),
            default=0,
        )
        candidates.append((score, person.gramps_id))
    candidates.sort(reverse=True)
    if not candidates or candidates[0][0] < 0.9:
        return None
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    if candidates[0][0] == 1:
        return candidates[0][1]
    if len(candidates) > 1 and candidates[0][0] < candidates[1][0] + 0.06:
        return None
    return candidates[0][1]
