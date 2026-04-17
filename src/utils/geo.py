import re
import math
from typing import Dict, Optional, Tuple

from cache import DEFAULT_CACHE, make_cache_key
from observability import get_logger

logger = get_logger("agriconnect.geo")

# -----------------------------
# State mappings (unchanged)
# -----------------------------
STATE_ALIASES: Dict[str, str] = {
    "iowa": "IA", "illinois": "IL", "nebraska": "NE", "minnesota": "MN",
    "missouri": "MO", "south dakota": "SD", "north dakota": "ND",
    "kansas": "KS", "wisconsin": "WI", "indiana": "IN", "ohio": "OH",
    "tennessee": "TN", "kentucky": "KY", "mississippi": "MS",
    "louisiana": "LA", "virginia": "VA", "north carolina": "NC",
}

CITY_STATE_LOOKUP: Dict[str, str] = {
    "des moines": "IA", "chicago": "IL", "omaha": "NE",
    "minneapolis": "MN", "st. louis": "MO", "kansas city": "MO",
    "sioux falls": "SD",
}

# -----------------------------
# NEW: Coordinates
# -----------------------------
CITY_COORDS: Dict[str, Tuple[float, float]] = {
    "des moines": (41.5868, -93.6250),
    "chicago": (41.8781, -87.6298),
    "omaha": (41.2565, -95.9345),
    "minneapolis": (44.9778, -93.2650),
    "st. louis": (38.6270, -90.1994),
    "kansas city": (39.0997, -94.5786),
    "sioux falls": (43.5446, -96.7311),
}

STATE_CENTROIDS: Dict[str, Tuple[float, float]] = {
    "IA": (42.0, -93.5),
    "IL": (40.0, -89.0),
    "NE": (41.5, -99.5),
    "MN": (46.0, -94.0),
    "MO": (38.5, -92.5),
    "SD": (44.5, -100.0),
    "ND": (47.5, -100.5),
}

CACHE_TTL = 24 * 60 * 60


def _resolve_state_only(location: str) -> str:
    if not location:
        return "IA"

    cleaned = location.strip().lower()

    city_state_match = re.match(r"^\s*([^,]+),\s*([A-Za-z]{2})\s*$", location)
    if city_state_match:
        return city_state_match.group(2).upper()

    if cleaned in STATE_ALIASES:
        return STATE_ALIASES[cleaned]

    if cleaned in CITY_STATE_LOOKUP:
        return CITY_STATE_LOOKUP[cleaned]

    if len(cleaned) == 2:
        return cleaned.upper()

    logger.warning(f"Unresolved location '{location}', defaulting to IA")
    return "IA"


def resolve_location(location: str) -> Tuple[str, float, float]:
    cleaned = location.strip().lower()
    cache_key = make_cache_key("geo", {"location": cleaned})

    cached = DEFAULT_CACHE.get(cache_key)
    if cached:
        return cached

    state = _resolve_state_only(location)

    if cleaned in CITY_COORDS:
        lat, lon = CITY_COORDS[cleaned]
    else:
        lat, lon = STATE_CENTROIDS.get(state, (42.0, -93.5))

    result = (state, lat, lon)
    DEFAULT_CACHE.set(cache_key, result, CACHE_TTL)
    return result


# -----------------------------
# NEW: Distance function
# -----------------------------
def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))