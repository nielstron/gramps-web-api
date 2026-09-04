"""Regression tests for uncertain dates at timeline boundaries."""

from gramps.gen.lib import Date

from gramps_webapi.api.resources.timeline import is_event_outside_date_window


def _about_estimated_year(year: int) -> Date:
    value = Date()
    value.set_yr_mon_day(year, 0, 0)
    value.set_modifier(Date.MOD_ABOUT)
    value.set_quality(Date.QUAL_ESTIMATED)
    return value


def _year(year: int) -> Date:
    value = Date()
    value.set_yr_mon_day(year, 0, 0)
    return value


def test_overlapping_uncertain_event_is_inside_life_window():
    birth = _about_estimated_year(1890)
    marriage = _about_estimated_year(1906)
    death = _year(1942)

    assert not is_event_outside_date_window(marriage, start_date=birth, end_date=death)


def test_event_entirely_outside_life_window_is_rejected():
    birth = _year(1890)
    death = _year(1942)

    assert is_event_outside_date_window(_year(1800), start_date=birth, end_date=death)
    assert is_event_outside_date_window(_year(2000), start_date=birth, end_date=death)
