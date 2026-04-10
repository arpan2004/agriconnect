"""
AMS MyMarketNews client.

Report structure (confirmed from slug 2850 - Iowa Daily Cash Grain Bids):

  GET /reports/2850
    - "Report Header" section (default)
    - results[] rows contain report metadata + report_narrative text
    - report_narrative holds state-average prices:
      "State Average Price: Corn -- $4.08 (-.39K) Down 2 cents  Soybeans -- $10.88 ..."

  GET /reports/2850?section=Report+Detail
    - "Report Detail" section
    - results[] rows hold per-elevator structured price fields

Strategy per slug:
  1. Fetch Report Detail  -> _parse_detail_section() for structured per-elevator rows
  2. If Detail has no usable rows, fetch Report Header
     -> _parse_header_section() extracts state-average from report_narrative
  3. Rows where report_narrative is null are silently skipped
  4. Deduplicate and return
"""

import asyncio
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from cache import DEFAULT_CACHE, make_cache_key
from observability import get_logger
from security import redact_secrets

BASE_URL = "https://marsapi.ams.usda.gov/services/v1.2"
TRANSPORT_URL = "https://www.ams.usda.gov/mnreports/sj_gr225.txt"

ALLOWED_DOMAINS = {"marsapi.ams.usda.gov", "www.ams.usda.gov", "ams.usda.gov"}

PRICES_TTL = 15 * 60
TRANSPORT_TTL = 6 * 60 * 60


# ---------------------------------------------------------------------------
# Static registry — populated from /reports index (April 2026).
# ---------------------------------------------------------------------------

_CORN_SLUGS: Dict[str, List[str]] = {
    "IA": ["2850"],   # Iowa Daily Cash Grain Bids
    "IL": ["3192"],   # Illinois Grain Bids
    "MN": ["3049"],   # Southern Minnesota Daily Grain Bids
    "NE": ["3225"],   # Nebraska Daily Elevator Grain Bids
    "IN": ["3463"],   # Indiana Grain Bids
    "OH": ["2851"],   # Ohio Daily Grain Bids
    "MO": ["2932"],   # Missouri Daily Grain Bids
    "KS": ["2886"],   # Kansas Daily Grain Bids
    "SD": ["3186"],   # South Dakota Daily Grain Bids
    "ND": ["3878"],   # North Dakota Daily Grain Bids
    "CO": ["2912"],   # Colorado Daily Grain Bids
    "WY": ["3239"],   # Wyoming Daily Grain Bids
    "MT": ["2771"],   # Montana Daily Elevator Grain Bids
    "TX": ["2711"],   # Texas Daily Grain Bids
    "OK": ["3100"],   # Oklahoma Daily Grain Bids
    "AR": ["2960"],   # Arkansas Daily Grain Bids
    "MS": ["2928"],   # Mississippi Daily Grain Bids
    "TN": ["3088"],   # Tennessee Daily Grain Bids
    "KY": ["2892"],   # Kentucky Daily Grain Bids
    "NC": ["3156"],   # North Carolina Cash Grain Bids
    "SC": ["2787"],   # South Carolina Daily Grain Bids
    "VA": ["3167"],   # Virginia Daily Grain Bids
    "MD": ["2714"],   # Maryland Grain Bids
    "PA": ["3091"],   # Pennsylvania Grain Bids
    "CA": ["3146"],   # California Grain Bids
    "OR": ["3148"],   # Portland Daily Grain Bids
    "WA": ["3148"],   # Portland covers WA elevators too
    "_barge":  ["3043"],   # Iowa-Southern MN Barge Terminal Grain Bids
    "_export": ["3147"],   # Louisiana and Texas Export Bids
}

_SOYBEAN_SLUGS: Dict[str, List[str]] = {
    "IA": ["2850"], "IL": ["3192"], "MN": ["3049"], "NE": ["3225"],
    "IN": ["3463"], "OH": ["2851"], "MO": ["2932"], "KS": ["2886"],
    "SD": ["3186"], "ND": ["3878"], "TX": ["2711"], "OK": ["3100"],
    "AR": ["2960"], "MS": ["2928"], "TN": ["3088"], "KY": ["2892"],
    "NC": ["3156"], "SC": ["2787"], "VA": ["3167"], "MD": ["2714"],
    "PA": ["3091"], "MT": ["2771"],
    "_barge":  ["3043"],
    "_export": ["3147"],
}

_WHEAT_SLUGS: Dict[str, List[str]] = {
    "KS": ["2886", "3223"],  # + KC Board of Trade Wheat
    "ND": ["3878", "3046"],  # + Minneapolis Daily Grain Report
    "MN": ["3049", "3046"],
    "SD": ["3186"], "MT": ["2771"], "CO": ["2912"], "WY": ["3239"],
    "NE": ["3225"], "OK": ["3100"], "TX": ["2711"],
    "OR": ["3148"], "WA": ["3148"],
    "IL": ["3192"], "OH": ["2851"], "IN": ["3463"],
    "VA": ["3167"], "PA": ["3091"],
    "_export": ["3147"],
}

GRAIN_REPORT_REGISTRY: Dict[str, Dict[str, List[str]]] = {
    "corn":     _CORN_SLUGS,
    "soybeans": _SOYBEAN_SLUGS,
    "soybean":  _SOYBEAN_SLUGS,
    "wheat":    _WHEAT_SLUGS,
}

# ---------------------------------------------------------------------------
# Commodity patterns
# ---------------------------------------------------------------------------

# Regex patterns to extract price from report_narrative.
# Narrative format: "Corn -- $4.08 (-.39K) Down 2 cents"
# Each pattern must have exactly one capture group for the price digits.
NARRATIVE_COMMODITY_PATTERNS: Dict[str, List[str]] = {
    "corn":     [r"corn\s*--\s*\$?([\d.]+)"],
    "soybeans": [r"soybeans?\s*--\s*\$?([\d.]+)"],
    "soybean":  [r"soybeans?\s*--\s*\$?([\d.]+)"],
    "wheat":    [r"wheat\s*--\s*\$?([\d.]+)", r"hard\s+red\s+winter\s*--\s*\$?([\d.]+)"],
}

# Substrings for filtering rows in the Report Detail structured section.
DETAIL_COMMODITY_KEYWORDS: Dict[str, List[str]] = {
    "corn":     ["corn"],
    "soybeans": ["soybean", "soybeans"],
    "soybean":  ["soybean", "soybeans"],
    "wheat":    ["wheat"],
}

# ---------------------------------------------------------------------------
# Fallback sample data
# ---------------------------------------------------------------------------

SAMPLE_PRICES = [
    {"location_name": "Des Moines", "state": "IA", "market_type": "elevator",
     "cash_price": 4.82, "basis": -0.05, "report_date": "2026-03-01",
     "data_source": "USDA AMS [SAMPLE]"},
    {"location_name": "Chicago", "state": "IL", "market_type": "terminal",
     "cash_price": 4.95, "basis": 0.08, "report_date": "2026-03-01",
     "data_source": "USDA AMS [SAMPLE]"},
    {"location_name": "Omaha", "state": "NE", "market_type": "processor",
     "cash_price": 4.77, "basis": -0.12, "report_date": "2026-03-01",
     "data_source": "USDA AMS [SAMPLE]"},
]

SAMPLE_TRANSPORT = [
    {"mode": "truck", "origin_region": "IA", "destination": "Chicago IL",
     "rate_per_bushel": 0.28, "note": "Sample truck rate"},
    {"mode": "rail", "origin_region": "IA", "destination": "Gulf Export",
     "rate_per_bushel": 0.35, "note": "Sample rail rate"},
    {"mode": "barge", "origin_region": "IA", "destination": "St. Louis MO",
     "rate_per_bushel": 0.22, "note": "Sample barge rate"},
]

logger = get_logger("agriconnect.ams")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _is_allowed(url: str) -> bool:
    return urlparse(url).netloc in ALLOWED_DOMAINS


def _float_from(item: Dict[str, Any], keys: List[str]) -> Optional[float]:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        try:
            return float(str(value).replace(",", "").strip())
        except (ValueError, AttributeError):
            continue
    return None


def _slugs_for(commodity: str, state: str) -> List[str]:
    registry = GRAIN_REPORT_REGISTRY.get(commodity.lower(), {})
    state_slugs = registry.get(state.upper(), [])
    barge_slugs = registry.get("_barge", [])
    export_slugs = registry.get("_export", [])

    seen: set = set()
    result: List[str] = []
    for slug in state_slugs + barge_slugs + export_slugs:
        if slug not in seen:
            seen.add(slug)
            result.append(slug)
    return result


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

async def _fetch_with_retry(
    url: str,
    params: Dict[str, Any],
    headers: Dict[str, str],
    timeout: float,
    auth: Optional[Tuple[str, str]] = None,
) -> httpx.Response:
    if not _is_allowed(url):
        raise ConnectionError(f"Blocked by URL allowlist: {url}")

    last_error: Exception = ConnectionError("Unknown error")
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, params=params, headers=headers, auth=auth)

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                sleep_for = float(retry_after) if retry_after else 1.5 ** attempt
                logger.warning("Rate limited; sleeping %.1fs (attempt %d)", sleep_for, attempt)
                await asyncio.sleep(sleep_for)
                continue

            if 400 <= response.status_code < 500:
                raise ConnectionError(f"HTTP {response.status_code}: {url}")

            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"Server error {response.status_code}",
                    request=response.request,
                    response=response,
                )

            if len(response.content) > 5 * 1024 * 1024:
                raise ConnectionError("Response exceeds 5 MB safety limit")

            return response

        except (httpx.TimeoutException, httpx.HTTPError, ConnectionError) as exc:
            last_error = exc
            if attempt >= 3:
                break
            await asyncio.sleep(1.5 ** attempt)

    raise ConnectionError(str(last_error))


# ---------------------------------------------------------------------------
# Parser A: Report Detail section — structured per-elevator rows
# ---------------------------------------------------------------------------

_DETAIL_COMMODITY_FIELDS = [
    "commodity_long_name", "commodity", "commodity_name",
    "class_", "grade", "item_description",
]
_DETAIL_LOCATION_FIELDS = [
    "location_name", "location", "office_name", "office",
    "market", "market_name", "point_of_sale_city_name",
    "company", "facility_name",
]
_DETAIL_STATE_FIELDS = [
    "state", "state_code", "state_alpha", "office_state", "location_state",
]
_DETAIL_PRICE_FIELDS = [
    "cash_price", "cashPrice", "bid", "bid_price",
    "price", "price_value", "weighted_avg_price", "avg_price", "average_price",
]
_DETAIL_BASIS_FIELDS = ["basis", "basis_value", "basis_num"]
_DETAIL_DATE_FIELDS = [
    "report_date", "reportDate", "date",
    "published_date", "report_date_time", "report_begin_date",
]
_DETAIL_TYPE_FIELDS = ["market_type", "type", "office_type", "facility_type"]


def _detail_commodity_matches(item: Dict[str, Any], commodity: str) -> bool:
    keywords = DETAIL_COMMODITY_KEYWORDS.get(commodity.lower(), [commodity.lower()])
    for field in _DETAIL_COMMODITY_FIELDS:
        value = item.get(field)
        if value is None:
            continue
        if any(kw in str(value).lower() for kw in keywords):
            return True
        return False  # field present but different commodity
    return True  # no commodity field — report is pre-filtered


def _parse_detail_section(
    payload: Dict[str, Any],
    commodity: str,
    state: str,
    slug: str,
) -> List[Dict[str, Any]]:
    rows = payload.get("results") or payload.get("data") or payload.get("items") or []
    if not isinstance(rows, list):
        return []

    report_level_date = next(
        (str(payload[dk]) for dk in _DETAIL_DATE_FIELDS if payload.get(dk)), ""
    )

    parsed: List[Dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        if not _detail_commodity_matches(item, commodity):
            continue

        cash_price = _float_from(item, _DETAIL_PRICE_FIELDS)
        if cash_price is None or cash_price == 0.0:
            continue

        parsed.append({
            "location_name": next((str(item[f]) for f in _DETAIL_LOCATION_FIELDS if item.get(f)), "Unknown"),
            "state":         next((str(item[f]) for f in _DETAIL_STATE_FIELDS if item.get(f)), state),
            "market_type":   next((str(item[f]) for f in _DETAIL_TYPE_FIELDS if item.get(f)), "elevator"),
            "cash_price":    cash_price,
            "basis":         _float_from(item, _DETAIL_BASIS_FIELDS) or 0.0,
            "report_date":   next((str(item[f]) for f in _DETAIL_DATE_FIELDS if item.get(f)), report_level_date),
            "data_source":   f"USDA AMS ({slug})",
        })

    return parsed


# ---------------------------------------------------------------------------
# Parser B: Report Header narrative — state-average fallback
#
# Narrative examples (| or \n as delimiter between commodities):
#   "State Average Price: Corn -- $4.08 (-.39K) Down 2 cents  Soybeans -- $10.88 ..."
#   "State Average Price: Corn -- $4.10 (-.39K) Down 4 cents | Soybeans -- $10.83 ..."
#
# report_narrative can be null — those rows are skipped silently.
# Multiple rows = multiple dates; we take only the most recent (first row).
# ---------------------------------------------------------------------------

def _extract_price_from_narrative(narrative: str, commodity: str) -> Optional[float]:
    """
    Pull the first price for *commodity* out of a report_narrative string.
    Returns None if not found or narrative is empty/null.
    """
    if not narrative:
        return None

    for pattern in NARRATIVE_COMMODITY_PATTERNS.get(commodity.lower(), []):
        match = re.search(pattern, narrative, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except (ValueError, IndexError):
                continue
    return None


def _parse_header_section(
    payload: Dict[str, Any],
    commodity: str,
    state: str,
    slug: str,
) -> List[Dict[str, Any]]:
    """
    Parse Report Header rows, extracting commodity prices from report_narrative.
    Null narratives are silently skipped.
    Returns one entry per row that yields a price (multiple = multiple dates).
    """
    rows = payload.get("results") or payload.get("data") or []
    if not isinstance(rows, list):
        return []

    parsed: List[Dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue

        narrative: Optional[str] = item.get("report_narrative")
        if not narrative:
            # Null or empty — nothing to parse, not an error
            continue

        price = _extract_price_from_narrative(narrative, commodity)
        if price is None:
            logger.debug(
                "Slug %s: no %r price in narrative: %.120s",
                slug, commodity, narrative,
            )
            continue

        location_city = item.get("office_city") or state
        row_state = item.get("office_state") or state
        report_date = (
            item.get("report_date")
            or item.get("report_begin_date")
            or item.get("published_date")
            or ""
        )

        parsed.append({
            "location_name": f"{location_city} (State Avg)",
            "state":         row_state,
            "market_type":   "state_average",
            "cash_price":    price,
            "basis":         0.0,
            "report_date":   report_date,
            "data_source":   f"USDA AMS ({slug})",
        })

    return parsed


# ---------------------------------------------------------------------------
# Single-slug orchestrator
# ---------------------------------------------------------------------------

async def _fetch_slug_prices(
    slug: str,
    commodity: str,
    state: str,
    auth: Tuple[str, str],
) -> List[Dict[str, Any]]:
    """
    Try Report Detail first, then Header narrative fallback.
    Returns an empty list if both paths yield nothing.
    """
    base_url = f"{BASE_URL}/reports/{slug}"

    # --- Step 1: Report Detail (structured rows) ---
    detail_cache_key = make_cache_key(base_url, {"section": "Report Detail"})
    cached = DEFAULT_CACHE.get(detail_cache_key)
    if cached is not None:
        logger.debug("Cache hit (Detail) slug=%s", slug)
        return cached

    detail_rows: List[Dict[str, Any]] = []
    try:
        resp = await _fetch_with_retry(
            base_url, {"section": "Report Detail"}, {}, timeout=12.0, auth=auth
        )
        detail_rows = _parse_detail_section(resp.json(), commodity, state, slug)
    except Exception as exc:
        logger.warning("Detail fetch failed slug=%s: %s", slug, redact_secrets(str(exc)))

    if detail_rows:
        DEFAULT_CACHE.set(detail_cache_key, detail_rows, PRICES_TTL)
        logger.info("slug=%s (Detail) -> %d row(s) for %s/%s", slug, len(detail_rows), commodity, state)
        return detail_rows

    logger.debug("slug=%s Detail empty for commodity=%r, trying Header narrative", slug, commodity)

    # --- Step 2: Report Header (narrative fallback) ---
    header_cache_key = make_cache_key(base_url, {})
    cached = DEFAULT_CACHE.get(header_cache_key)
    if cached is not None:
        logger.debug("Cache hit (Header) slug=%s", slug)
        return cached

    header_rows: List[Dict[str, Any]] = []
    try:
        resp = await _fetch_with_retry(base_url, {}, {}, timeout=12.0, auth=auth)
        header_rows = _parse_header_section(resp.json(), commodity, state, slug)
    except Exception as exc:
        logger.warning("Header fetch failed slug=%s: %s", slug, redact_secrets(str(exc)))
        return []

    if header_rows:
        # Cache only the most recent (first) row — older dates aren't useful for live prices
        most_recent = header_rows[:1]
        DEFAULT_CACHE.set(header_cache_key, most_recent, PRICES_TTL)
        logger.info("slug=%s (Header narrative) -> state avg %.2f for %s/%s",
                    slug, most_recent[0]["cash_price"], commodity, state)
        return most_recent

    logger.debug("slug=%s: no prices in Detail or Header for commodity=%r", slug, commodity)
    return []


# ---------------------------------------------------------------------------
# Public API: grain prices
# ---------------------------------------------------------------------------

async def fetch_grain_prices(commodity: str, state: str) -> List[Dict[str, Any]]:
    """
    Fetch grain cash prices for *commodity* in *state* from USDA AMS.

    Raises ConnectionError on total failure so callers can use fallback_prices().
    """
    api_key = os.getenv("USDA_AMS_API_KEY", "")
    if not api_key:
        logger.warning("USDA_AMS_API_KEY not set.")
        raise ConnectionError("Missing USDA AMS API key")

    auth: Tuple[str, str] = (api_key, "")

    slugs = _slugs_for(commodity, state)
    if not slugs:
        logger.warning("No slugs for commodity=%r state=%r.", commodity, state)
        raise ConnectionError(
            f"No AMS report registered for commodity='{commodity}' state='{state}'"
        )

    logger.info("commodity=%r state=%r slugs=%s", commodity, state, slugs)

    combined: List[Dict[str, Any]] = []
    seen_keys: set = set()
    empty_count = 0

    for slug in slugs:
        rows = await _fetch_slug_prices(slug, commodity, state, auth)
        if not rows:
            empty_count += 1
            continue
        for entry in rows:
            key = (entry["location_name"], entry["cash_price"])
            if key not in seen_keys:
                seen_keys.add(key)
                combined.append(entry)

    if not combined:
        raise ConnectionError(
            f"No price data returned for commodity='{commodity}' state='{state}' "
            f"({empty_count}/{len(slugs)} slugs empty)"
        )

    return combined


# ---------------------------------------------------------------------------
# Public API: transport
# ---------------------------------------------------------------------------

async def fetch_transport_report() -> List[Dict[str, Any]]:
    cache_key = make_cache_key(TRANSPORT_URL, {})
    cached = DEFAULT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        response = await _fetch_with_retry(TRANSPORT_URL, {}, {}, timeout=12.0)
        parsed = _parse_transport(response.text)
        if parsed:
            DEFAULT_CACHE.set(cache_key, parsed, TRANSPORT_TTL)
        return parsed
    except Exception as exc:
        logger.warning("Transport fetch failed: %s", redact_secrets(str(exc)))
        raise ConnectionError("AMS transport fetch failed") from exc


def _parse_transport(text: str) -> List[Dict[str, Any]]:
    rates: List[Dict[str, Any]] = []
    pattern = re.compile(r"^(TRUCK|RAIL|BARGE)\s+([A-Z]{2})\s+(.+?)\s+(\d+\.\d+)")
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        mode, origin, destination, rate = match.groups()
        rates.append({
            "mode":            mode.lower(),
            "origin_region":   origin,
            "destination":     destination.strip(),
            "rate_per_bushel": float(rate),
            "note":            "",
        })
    return rates


# ---------------------------------------------------------------------------
# Fallbacks
# ---------------------------------------------------------------------------

def fallback_prices() -> List[Dict[str, Any]]:
    return [dict(e) for e in SAMPLE_PRICES]


def fallback_transport() -> List[Dict[str, Any]]:
    return [dict(e) for e in SAMPLE_TRANSPORT]


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def supported_states(commodity: str) -> List[str]:
    return [k for k in GRAIN_REPORT_REGISTRY.get(commodity.lower(), {}) if not k.startswith("_")]


def is_supported(commodity: str, state: str) -> bool:
    return bool(_slugs_for(commodity, state))