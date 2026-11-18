"""SF Bay Area rules shared by the pipeline scripts, so jobs and funding filings are
checked against the same ZIP prefixes and city names."""

from __future__ import annotations

from typing import Any

# First three digits of ZIP codes in the nine Bay Area counties. 950 and 954 also
# reach a few places outside them (Santa Cruz, Hollister, Ukiah), and Vacaville and
# Dixon (956) are missed; both are rare homes for AI startups.
BAY_AREA_ZIP3: tuple[str, ...] = (
    "940", "941", "943", "944", "945", "946", "947", "948", "949", "950", "951", "954",
)

# Bay Area city names, matched as parts of a city name ("san francisco" also matches
# "South San Francisco"). Step 1 sends them to TheirStack's HQ city filter; step 3 uses
# them on EDGAR's business city when a Form D address is outside California.
BAY_AREA_CITY_PATTERNS: tuple[str, ...] = (
    "san francisco", "oakland", "berkeley", "emeryville", "alameda",
    "hayward", "fremont", "pleasanton", "walnut creek", "san ramon",
    "palo alto", "menlo park", "mountain view", "sunnyvale", "santa clara",
    "san jose", "cupertino", "los altos", "los gatos", "campbell",
    "milpitas", "redwood city", "redwood shores", "san mateo", "burlingame",
    "san carlos", "belmont", "foster city", "san bruno", "millbrae",
    "mill valley", "sausalito", "san rafael",
)


def hq_zip_check(postal_code: Any) -> str:
    """Return "bay_area", "outside" or "missing" for a company's HQ postal code."""
    code = str(postal_code or "").strip()
    if not code:
        return "missing"
    return "bay_area" if code[:3] in BAY_AREA_ZIP3 else "outside"


def is_bay_area_city(location: Any) -> bool:
    """True if a "City, ST" location is in California and its city matches a Bay Area name."""
    city, _, state = str(location or "").rpartition(",")
    return state.strip().upper() == "CA" and is_bay_area_city_name(city)


def is_bay_area_city_name(city: Any) -> bool:
    """True if a city name without a state matches a Bay Area name, as TheirStack's HQ
    city filter does; a same-named city in another state also matches."""
    name = str(city or "").strip().lower()
    return bool(name) and any(pattern in name for pattern in BAY_AREA_CITY_PATTERNS)
