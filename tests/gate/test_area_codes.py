"""Tests for area-code timezone derivation (livekit_outbound/gate/area_codes.py)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from livekit_outbound.gate.area_codes import (
    area_code,
    known_area_codes,
    timezones_for_area_code,
    timezones_for_number,
)


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("+12125550100", "212"),
        ("+13125550100", "312"),
        ("+14165550100", "416"),
        ("+442071234567", None),  # United Kingdom: not NANP
        ("+15550100", None),  # too short
        ("+11235550100", None),  # area code cannot start with 0 or 1
        ("12125550100", None),  # not E.164
    ],
)
def test_area_code_extraction(number, expected):
    assert area_code(number) == expected


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("+12125550100", ("America/New_York",)),
        ("+13125550100", ("America/Chicago",)),
        ("+13035550100", ("America/Denver",)),
        ("+16025550100", ("America/Phoenix",)),
        ("+12135550100", ("America/Los_Angeles",)),
        ("+19075550100", ("America/Anchorage",)),
        ("+18085550100", ("Pacific/Honolulu",)),
        ("+14165550100", ("America/Toronto",)),
        ("+13065550100", ("America/Regina",)),
        ("+17095550100", ("America/St_Johns", "America/Goose_Bay")),
        ("+18505550100", ("America/New_York", "America/Chicago")),
        ("+18005550100", ()),  # toll-free: no location
        ("+442071234567", ()),  # non-NANP
    ],
)
def test_timezones_for_number(number, expected):
    assert timezones_for_number(number) == expected


def test_every_zone_in_the_table_is_a_valid_iana_name():
    for code in known_area_codes():
        zones = timezones_for_area_code(code)
        assert zones, code
        for zone in zones:
            ZoneInfo(zone)  # raises if unknown


def test_table_covers_the_major_metro_area_codes():
    # A representative spot check: one code per state/province is enough to
    # catch a wholesale omission without freezing the whole table in a test.
    for code in ("212", "312", "213", "404", "713", "305", "602", "206", "617", "416", "604"):
        assert code in known_area_codes(), code
