"""Timezone of a dialed number, derived from its NANP area code.

The calling-hours rule is defined in terms of local time *at the called
party's location*, so the gate must never evaluate it against the server's
clock, or even against the contact's home timezone on file, when the number
being dialed says otherwise. For North American Numbering Plan numbers
(``+1`` followed by ten digits) the area code pins the location well enough
to pick an IANA zone.

A handful of area codes straddle a timezone boundary (for example 850 in
the Florida panhandle spans Eastern and Central). Those map to *every*
candidate zone, and the composite gate requires the calling window to hold
in all of them -- the conservative reading, since no single choice is safe
at both edges of the window. Toll-free, premium, and non-NANP numbers map
to nothing, and the gate falls back to the contact's recorded timezone.

Zone names are the canonical IANA identifiers as shipped by ``tzdata``;
several U.S. regions have their own identifiers (Detroit, Indianapolis,
Louisville, Phoenix, Boise) because their daylight-saving history differs
from the neighbouring zone even where the current offset matches.
"""

from __future__ import annotations

import re

NANP_NUMBER = re.compile(r"^\+1([2-9]\d{2})[2-9]\d{6}$")

_ZONES: dict[str, tuple[str, ...]] = {}


def _assign(zones: str | tuple[str, ...], *codes: int) -> None:
    zone_tuple = (zones,) if isinstance(zones, str) else tuple(zones)
    for code in codes:
        key = f"{code:03d}"
        if key in _ZONES:
            raise RuntimeError(f"area code {key} assigned twice in the timezone table")
        _ZONES[key] = zone_tuple


# --- United States: Eastern ------------------------------------------------
_assign(
    "America/New_York",
    # Connecticut
    203, 475, 860, 959,
    # Delaware
    302,
    # District of Columbia
    202, 771,
    # Florida (peninsula)
    239, 305, 321, 352, 386, 407, 448, 561, 645, 656, 689, 727, 754, 772, 786,
    813, 863, 904, 941, 954,
    # Georgia
    229, 404, 470, 478, 678, 706, 762, 770, 912, 943,
    # Kentucky (eastern)
    606, 859,
    # Maine
    207,
    # Maryland
    227, 240, 301, 410, 443, 667,
    # Massachusetts
    339, 351, 413, 508, 617, 774, 781, 857, 978,
    # New Hampshire
    603,
    # New Jersey
    201, 551, 609, 640, 732, 848, 856, 862, 908, 973,
    # New York
    212, 315, 329, 332, 347, 363, 516, 518, 585, 607, 624, 631, 646, 680, 716,
    718, 838, 845, 914, 917, 929, 934,
    # North Carolina
    252, 336, 704, 743, 828, 910, 919, 980, 984,
    # Ohio
    216, 220, 234, 283, 326, 330, 380, 419, 436, 440, 513, 567, 614, 740, 937,
    # Pennsylvania
    215, 223, 267, 272, 412, 445, 484, 570, 582, 610, 717, 724, 814, 835, 878,
    # Rhode Island
    401,
    # South Carolina
    803, 821, 839, 843, 854, 864,
    # Tennessee (eastern)
    423, 865,
    # Vermont
    802,
    # Virginia
    276, 434, 540, 571, 686, 703, 757, 804, 826, 948,
    # West Virginia
    304, 681,
)
_assign("America/Kentucky/Louisville", 502)
_assign("America/Indiana/Indianapolis", 260, 317, 463, 574, 765)
_assign("America/Detroit", 231, 248, 269, 313, 517, 586, 616, 679, 734, 810, 947, 989)

# --- United States: Central ------------------------------------------------
_assign(
    "America/Chicago",
    # Alabama
    205, 251, 256, 334, 659, 938,
    # Arkansas
    327, 479, 501, 870,
    # Illinois
    217, 224, 309, 312, 331, 447, 464, 618, 630, 708, 730, 773, 779, 815, 847, 872,
    # Indiana (northwest)
    219,
    # Iowa
    319, 515, 563, 641, 712,
    # Kansas
    316, 620, 785, 913,
    # Kentucky (western)
    270, 364,
    # Louisiana
    225, 318, 337, 457, 504, 985,
    # Minnesota
    218, 320, 507, 612, 651, 763, 952,
    # Mississippi
    228, 601, 662, 769,
    # Missouri
    235, 314, 417, 557, 573, 636, 660, 816, 975,
    # Nebraska (eastern)
    402, 531,
    # Oklahoma
    405, 539, 572, 580, 918,
    # Tennessee (central and western)
    615, 629, 731, 901, 931,
    # Texas (all but El Paso)
    210, 214, 254, 281, 325, 346, 361, 409, 430, 432, 469, 512, 682, 713, 726,
    737, 806, 817, 830, 832, 903, 936, 940, 945, 956, 972, 979,
    # Wisconsin
    262, 274, 353, 414, 534, 608, 715, 920,
)

# --- United States: Mountain -----------------------------------------------
_assign(
    "America/Denver",
    # Colorado
    303, 719, 720, 970, 983,
    # Montana
    406,
    # New Mexico
    505, 575,
    # Texas (El Paso)
    915,
    # Utah
    385, 435, 801,
    # Wyoming
    307,
)
# Arizona does not observe daylight saving time.
_assign("America/Phoenix", 480, 520, 602, 623)

# --- United States: Pacific --------------------------------------------------
_assign(
    "America/Los_Angeles",
    # California
    209, 213, 279, 310, 323, 341, 350, 369, 408, 415, 424, 442, 510, 530, 559,
    562, 619, 626, 628, 650, 657, 661, 669, 707, 714, 747, 760, 805, 818, 820,
    831, 837, 840, 858, 909, 916, 925, 949, 951,
    # Nevada
    702, 725, 775,
    # Oregon
    458, 503, 541, 971,
    # Washington
    206, 253, 360, 425, 509, 564,
)

# --- United States: Alaska, Hawaii, territories ------------------------------
_assign("America/Anchorage", 907)
_assign("Pacific/Honolulu", 808)
_assign("America/Puerto_Rico", 340, 787, 939)
_assign("Pacific/Guam", 671)
_assign("Pacific/Saipan", 670)
_assign("Pacific/Pago_Pago", 684)

# --- United States: area codes that straddle a timezone boundary -------------
_assign(("America/New_York", "America/Chicago"), 850)  # Florida panhandle
_assign(("America/Indiana/Indianapolis", "America/Chicago"), 812, 930)  # southern Indiana
_assign(("America/Detroit", "America/Menominee"), 906)  # Michigan Upper Peninsula
_assign(("America/Chicago", "America/Denver"), 308, 605, 701)  # NE, SD, ND
_assign(("America/Boise", "America/Los_Angeles"), 208, 986)  # Idaho
_assign(("America/Phoenix", "America/Denver"), 928)  # northern Arizona incl. Navajo Nation

# --- Canada ------------------------------------------------------------------
_assign(
    "America/Toronto",
    # Ontario
    226, 249, 289, 343, 365, 382, 416, 437, 519, 548, 613, 647, 683, 705, 742,
    753, 905, 942,
    # Quebec
    263, 354, 367, 418, 438, 450, 468, 514, 579, 581, 819, 873,
)
_assign("America/Winnipeg", 204, 431, 584)
# Saskatchewan does not observe daylight saving time.
_assign("America/Regina", 306, 474, 639)
_assign("America/Edmonton", 368, 403, 587, 780, 825)
_assign("America/Vancouver", 236, 604, 672, 778)
_assign("America/Halifax", 782, 902)
_assign("America/Moncton", 428, 506)
_assign(("America/Toronto", "America/Winnipeg"), 807)  # northwestern Ontario
_assign(("America/Vancouver", "America/Edmonton"), 250)  # BC interior / Peace River
_assign(("America/St_Johns", "America/Goose_Bay"), 709)  # Newfoundland / Labrador
_assign(("America/Whitehorse", "America/Edmonton", "America/Iqaluit"), 867)  # territories


def area_code(number: str) -> str | None:
    """The three-digit area code of an E.164 NANP number, else None."""
    match = NANP_NUMBER.match(number)
    return match.group(1) if match else None


def timezones_for_area_code(code: str) -> tuple[str, ...]:
    """IANA zones an area code can fall in (usually one), or ``()`` if unknown."""
    return _ZONES.get(code, ())


def timezones_for_number(number: str) -> tuple[str, ...]:
    """IANA zones a dialed number can fall in, or ``()`` if not derivable.

    Empty for non-NANP numbers, toll-free and other non-geographic codes,
    and any geographic code missing from the table.
    """
    code = area_code(number)
    if code is None:
        return ()
    return timezones_for_area_code(code)


def known_area_codes() -> frozenset[str]:
    return frozenset(_ZONES)
