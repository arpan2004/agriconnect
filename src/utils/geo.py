import re
from typing import Dict, Optional

from cache import DEFAULT_CACHE, make_cache_key
from observability import get_logger

logger = get_logger("agriconnect.geo")

STATE_ALIASES: Dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}

CITY_STATE_LOOKUP: Dict[str, str] = {
    "ames": "IA",
    "des moines": "IA",
    "cedar rapids": "IA",
    "sioux city": "IA",
    "chicago": "IL",
    "peoria": "IL",
    "st. louis": "MO",
    "kansas city": "MO",
    "omaha": "NE",
    "lincoln": "NE",
    "fargo": "ND",
    "bismarck": "ND",
    "sioux falls": "SD",
    "minneapolis": "MN",
    "springfield": "IL",
    "wichita": "KS",
    "topeka": "KS",
    "columbus": "OH",
    "indianapolis": "IN",
    "memphis": "TN",
    "louisville": "KY",
    "gulfport": "MS",
    "new orleans": "LA",
}

ZIP_PREFIX_RANGES = [
    (500, 528, "IA"),
    (600, 629, "IL"),
    (630, 658, "MO"),
    (660, 679, "KS"),
    (680, 693, "NE"),
    (550, 567, "MN"),
    (570, 577, "SD"),
    (580, 588, "ND"),
    (430, 459, "OH"),
    (460, 479, "IN"),
    (370, 385, "TN"),
]

CACHE_TTL = 24 * 60 * 60


def _state_from_zip(zip_code: str) -> Optional[str]:
    try:
        prefix = int(zip_code[:3])
    except ValueError:
        return None
    for start, end, state in ZIP_PREFIX_RANGES:
        if start <= prefix <= end:
            return state
    return None


def resolve_location(location: str) -> str:
    if not location:
        return "IA"

    cleaned = location.strip()
    cache_key = make_cache_key("geo", {"location": cleaned.lower()})
    cached = DEFAULT_CACHE.get(cache_key)
    if cached:
        return cached

    lower = cleaned.lower()

    city_state_match = re.match(r"^\s*([^,]+),\s*([a-zA-Z]{2})\s*$", cleaned)
    if city_state_match:
        state = city_state_match.group(2).upper()
        DEFAULT_CACHE.set(cache_key, state, CACHE_TTL)
        return state

    city_state_name = re.match(r"^\s*([^,]+),\s*([a-zA-Z\s]+)\s*$", cleaned)
    if city_state_name:
        state_name = city_state_name.group(2).strip().lower()
        state = STATE_ALIASES.get(state_name)
        if state:
            DEFAULT_CACHE.set(cache_key, state, CACHE_TTL)
            return state

    if re.match(r"^\d{5}$", cleaned):
        state = _state_from_zip(cleaned)
        if state:
            DEFAULT_CACHE.set(cache_key, state, CACHE_TTL)
            return state

    if lower in STATE_ALIASES:
        state = STATE_ALIASES[lower]
        DEFAULT_CACHE.set(cache_key, state, CACHE_TTL)
        return state

    if len(cleaned) == 2 and cleaned.isalpha():
        state = cleaned.upper()
        DEFAULT_CACHE.set(cache_key, state, CACHE_TTL)
        return state

    city_state = CITY_STATE_LOOKUP.get(lower)
    if city_state:
        DEFAULT_CACHE.set(cache_key, city_state, CACHE_TTL)
        return city_state

    logger.warning(f"Unresolved location '{cleaned}', defaulting to IA")
    DEFAULT_CACHE.set(cache_key, "IA", CACHE_TTL)
    return "IA"
