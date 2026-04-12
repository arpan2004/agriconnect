"""
USDA AgTransport Socrata client.

Platform:  https://agtransport.usda.gov
API docs:  https://dev.socrata.com/docs/queries/
No API key required. Set SOCRATA_APP_TOKEN to raise rate limit to 1000 req/sec.

Column names confirmed from live API error responses (April 2026):

  deqi-uken  Downbound Grain Barge Rates
             date, week, month, year, location, rate
             'rate' = % of 1976 tariff benchmark (spot/nearby)

  fxkn-2w9c  Quarterly Grain Truck Rates
             Fetched without $select — columns discovered at runtime.

  8uye-ieij  Grain Transportation Cost Indicators
             Fetched without $select — columns discovered at runtime.

  an4w-mnp7  Grain Price Spreads
             Fetched without $select — columns discovered at runtime.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from cache import DEFAULT_CACHE, make_cache_key
from observability import get_logger
from security import redact_secrets

SOCRATA_BASE = "https://agtransport.usda.gov/resource"

DATASET_BARGE_SPOT        = "deqi-uken"  # Downbound Grain Barge Rates (spot)
DATASET_BARGE_ONE_MONTH   = "svms-9yya"  # One Month Forward
DATASET_BARGE_THREE_MONTH = "uuhv-5etw"  # Three Month Forward
DATASET_TRUCK_RATES       = "fxkn-2w9c"  # Quarterly Grain Truck Rates
DATASET_COST_INDICATORS   = "8uye-ieij"  # Weekly Transport Cost Indices
DATASET_PRICE_SPREADS     = "an4w-mnp7"  # Grain Price Spreads

TRANSPORT_TTL = 6 * 60 * 60  # 6 hours — data updates weekly

# ---------------------------------------------------------------------------
# Barge rate conversion: % of 1976 tariff benchmark → $/bushel
#
# Formula: (rate_pct / 100) * benchmark_$/ton  /  bu_per_ton
# Benchmark $/ton from AMS GTR methodology (St. Louis = $3.99 confirmed).
# ---------------------------------------------------------------------------
# Source: USDA AMS dataset deqi-uken description + Grain Transportation Reports
# Benchmarks are 1976 Tariff No. 7 rates in $/SHORT TON (ton = 2,000 lbs)
_BENCHMARK_PER_TON: Dict[str, float] = {
    "Twin Cities":     6.19,   # Upper Mississippi
    "Mid-Mississippi": 5.32,   # Eastern IA / Western IL stretch
    "Illinois":        4.64,   # Lower Illinois River
    "St. Louis":       3.99,   # Confirmed: AMS GTR + dataset description
    "Cincinnati":      4.69,   # Middle third of Ohio River
    "Lower Ohio":      4.46,   # Final third of Ohio River
    "Cairo-Memphis":   3.14,   # Cairo, IL to Memphis, TN
}
_DEFAULT_BENCHMARK = 4.64   # Illinois — most central Midwest fallback

# Bushels per SHORT TON (ton = 2,000 lbs) per AMS GTR methodology
# Corn: 56 lbs/bu → 2000/56 = 35.714
# Soybeans/Wheat: 60 lbs/bu → 2000/60 = 33.333
_BU_PER_TON: Dict[str, float] = {
    "corn":     35.714,
    "soybeans": 33.333,
    "soybean":  33.333,
    "wheat":    33.333,
}
_DEFAULT_BU_PER_TON = 33.333

# ---------------------------------------------------------------------------
# State → segment / region / origin mappings
# ---------------------------------------------------------------------------
STATE_TO_BARGE_SEGMENT: Dict[str, str] = {
    "MN": "Twin Cities",   "WI": "Twin Cities",
    "ND": "Twin Cities",   "SD": "Twin Cities",
    "IA": "Mid-Mississippi",
    "IL": "Illinois",
    "IN": "Mid-Mississippi",
    "OH": "Lower Ohio",    "KY": "Lower Ohio",
    "MO": "St. Louis",
    "AR": "Cairo-Memphis", "TN": "Cairo-Memphis", "MS": "Cairo-Memphis",
}

STATE_TO_TRUCK_REGION: Dict[str, str] = {
    "IA": "North Central", "IL": "North Central", "IN": "North Central",
    "MN": "North Central", "MO": "North Central", "NE": "North Central",
    "OH": "North Central", "SD": "North Central", "ND": "North Central",
    "WI": "North Central", "KS": "Southern Plains", "OK": "Southern Plains",
    "TX": "Southern Plains", "CO": "Mountain",    "WY": "Mountain",
    "MT": "Mountain",       "ID": "Mountain",
    "WA": "Pacific",        "OR": "Pacific",       "CA": "Pacific",
    "AR": "Delta",          "MS": "Delta",         "TN": "Delta",
    "KY": "Appalachian",    "VA": "Appalachian",   "NC": "Appalachian",
}

STATE_TO_SPREAD_ORIGIN: Dict[str, str] = {
    "IA": "Iowa",       "IL": "Illinois",   "NE": "Nebraska",
    "KS": "Kansas",     "MN": "Minnesota",  "ND": "North Dakota",
    "SD": "South Dakota", "IN": "Indiana",  "OH": "Ohio",
    "MO": "Missouri",
}

COMMODITY_TO_SPREAD_NAME: Dict[str, str] = {
    "corn":     "Corn",
    "soybeans": "Soybeans",
    "soybean":  "Soybeans",
    "wheat":    "HRW Wheat",
}

# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------
SAMPLE_TRANSPORT = [
    {"mode": "barge",        "origin_region": "IA",
     "destination": "Gulf Export via Mid-Mississippi",
     "rate_per_bushel": 0.22, "note": "Sample barge rate [FALLBACK]",
     "source_dataset": "sample"},
    {"mode": "rail_or_barge","origin_region": "IA",
     "destination": "Gulf",
     "rate_per_bushel": 0.35, "note": "Sample implied spread Iowa→Gulf [FALLBACK]",
     "source_dataset": "sample"},
    {"mode": "truck",        "origin_region": "IA",
     "destination": "Regional Elevator",
     "rate_per_bushel": 0.28, "note": "Sample truck rate [FALLBACK]",
     "source_dataset": "sample"},
]

logger = get_logger("agriconnect.transport")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _headers() -> Dict[str, str]:
    token = os.getenv("SOCRATA_APP_TOKEN", "")
    return {"X-App-Token": token} if token else {}


async def _soda_get(
    dataset_id: str,
    params: Dict[str, Any],
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """Single SODA GET. Returns list of row dicts."""
    url = f"{SOCRATA_BASE}/{dataset_id}.json"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params, headers=_headers())

        if resp.status_code == 429:
            raise ConnectionError(
                f"Socrata rate limit on {dataset_id}. "
                "Set SOCRATA_APP_TOKEN env var to raise limit to 1000 req/sec."
            )
        if resp.status_code >= 400:
            raise ConnectionError(
                f"Socrata HTTP {resp.status_code} for dataset {dataset_id}: "
                f"{resp.text[:300]}"
            )
        data = resp.json()
        if not isinstance(data, list):
            raise ConnectionError(
                f"Unexpected response type {type(data)} from {dataset_id}"
            )
        return data

    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        raise ConnectionError(f"Socrata request failed for {dataset_id}: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_commodity(commodity: Optional[str]) -> str:
    """Normalise commodity — default to 'corn' if None/empty."""
    return (commodity or "corn").lower()


def _barge_pct_to_per_bushel(
    pct: Any,
    segment: str,
    commodity: str,
) -> Optional[float]:
    try:
        rate_pct = float(pct)
    except (TypeError, ValueError):
        return None
    benchmark  = _BENCHMARK_PER_TON.get(segment, _DEFAULT_BENCHMARK)
    bu_per_ton = _BU_PER_TON.get(commodity, _DEFAULT_BU_PER_TON)
    return round((rate_pct / 100) * benchmark / bu_per_ton, 4)


def _first(row: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the value of the first key that exists in *row*."""
    for k in keys:
        v = row.get(k)
        if v is not None:
            return v
    return default


# ---------------------------------------------------------------------------
# Dataset: Downbound Grain Barge Rates (deqi-uken)
#
# Confirmed columns from live API error response:
#   date, week, month, year, location, rate
#   'rate' = spot/nearby % of 1976 tariff benchmark
# ---------------------------------------------------------------------------

async def fetch_barge_rates(
    origin_state: str,
    commodity: Optional[str] = None,
    weeks: int = 4,
) -> List[Dict[str, Any]]:
    commodity = _safe_commodity(commodity)
    segment   = STATE_TO_BARGE_SEGMENT.get(origin_state.upper())
    if not segment:
        logger.debug("No barge segment mapped for state=%s", origin_state)
        return []

    cutoff = (
        datetime.now(tz=timezone.utc) - timedelta(weeks=weeks)
    ).strftime("%Y-%m-%dT00:00:00")

    params = {
        "$where":  f"date >= '{cutoff}' AND UPPER(location) = UPPER('{segment}')",
        "$order":  "date DESC",
        "$limit":  "8",
        # Only select confirmed-real columns
        "$select": "date,week,month,year,location,rate",
    }

    cache_key = make_cache_key(f"barge:{DATASET_BARGE_SPOT}:{segment}", {})
    cached = DEFAULT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    rows = await _soda_get(DATASET_BARGE_SPOT, params)

    enriched: List[Dict[str, Any]] = []
    for row in rows:
        rate_pct = row.get("rate")
        enriched.append({
            **row,
            "segment":         segment,
            "commodity":       commodity,
            "rate_per_bushel": _barge_pct_to_per_bushel(rate_pct, segment, commodity),
            "rate_pct":        rate_pct,
        })

    if enriched:
        DEFAULT_CACHE.set(cache_key, enriched, TRANSPORT_TTL)
        logger.info(
            "Barge: %d rows for segment=%s state=%s", len(enriched), segment, origin_state
        )
    return enriched


# ---------------------------------------------------------------------------
# Dataset: Quarterly Grain Truck Rates (fxkn-2w9c)
#
# Column names unknown — fetched without $select so Socrata returns all columns.
# We then probe for any price-like field.
# ---------------------------------------------------------------------------

# Candidate column names for the $/bu truck rate (we'll take the first match)
_TRUCK_RATE_CANDIDATES = [
    "long_haul_100mi", "long_haul_100_mi",
    "rate_per_bushel", "rate",
    "short_haul_25mi", "short_haul_25_mi",
    "long_haul_200mi", "long_haul_200_mi",
    "truck_rate", "cost_per_bushel",
]
_TRUCK_LABEL_CANDIDATES = [
    "long_haul_100mi", "long_haul_100_mi",
    "short_haul_25mi", "short_haul_25_mi",
    "long_haul_200mi", "long_haul_200_mi",
]


async def fetch_truck_rates(
    origin_state: str,
) -> List[Dict[str, Any]]:
    region = STATE_TO_TRUCK_REGION.get(origin_state.upper())
    if not region:
        logger.debug("No truck region mapped for state=%s", origin_state)
        return []

    cutoff = (
        datetime.now(tz=timezone.utc) - timedelta(days=365)
    ).strftime("%Y-%m-%dT00:00:00")

    params = {
        "$where": f"date >= '{cutoff}' AND UPPER(region) = UPPER('{region}')",
        "$order": "date DESC",
        "$limit": "4",
        # No $select — discover actual columns from the response
    }

    cache_key = make_cache_key(f"truck:{DATASET_TRUCK_RATES}:{region}", {})
    cached = DEFAULT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    rows = await _soda_get(DATASET_TRUCK_RATES, params)

    if rows:
        # Log actual column names once so you can update _TRUCK_RATE_CANDIDATES
        logger.info("Truck dataset columns: %s", list(rows[0].keys()))
        DEFAULT_CACHE.set(cache_key, rows, TRANSPORT_TTL)

    return rows


def _extract_truck_rate(row: Dict[str, Any]) -> tuple[Optional[float], str]:
    """
    Try candidate column names to find a $/bu truck rate.
    Returns (rate_float_or_None, label_string).
    """
    for col in _TRUCK_RATE_CANDIDATES:
        val = row.get(col)
        if val is not None:
            try:
                return float(val), col
            except (TypeError, ValueError):
                continue
    return None, "unknown"


# ---------------------------------------------------------------------------
# Dataset: Grain Transportation Cost Indicators (8uye-ieij)
#
# Fetched without $select — column names discovered at runtime.
# ---------------------------------------------------------------------------

_INDICATOR_CANDIDATES = {
    "truck":       ["truck_index", "truck_cost_index", "truck"],
    "rail":        ["unit_train_index", "rail_index", "shuttle_train_index"],
    "barge":       ["barge_index", "barge_cost_index"],
    "diesel":      ["diesel_price", "diesel"],
    "railcar_bid": ["secondary_railcar_bid_nearby", "secondary_railcar_bid",
                    "railcar_bid", "railcar_bid_nearby"],
}


async def fetch_cost_indicators(weeks: int = 4) -> List[Dict[str, Any]]:
    cutoff = (
        datetime.now(tz=timezone.utc) - timedelta(weeks=weeks)
    ).strftime("%Y-%m-%dT00:00:00")

    params = {
        "$where": f"date >= '{cutoff}'",
        "$order": "date DESC",
        "$limit": str(weeks + 5),
    }

    cache_key = make_cache_key(f"indicators:{DATASET_COST_INDICATORS}", params)
    cached = DEFAULT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        rows = await _soda_get(DATASET_COST_INDICATORS, params)
    except ConnectionError as exc:
        logger.warning("Cost indicators fetch failed: %s", str(exc))
        return []

    if rows:
        logger.info(
            "Cost indicators columns: %s", list(rows[0].keys())
        )
        DEFAULT_CACHE.set(cache_key, rows, TRANSPORT_TTL)
    return rows


# ---------------------------------------------------------------------------
# Dataset: Grain Price Spreads (an4w-mnp7)
# ---------------------------------------------------------------------------

async def fetch_price_spreads(
    commodity: Optional[str],
    origin_state: str,
    weeks: int = 4,
) -> List[Dict[str, Any]]:
    commodity = _safe_commodity(commodity)
    origin = STATE_TO_SPREAD_ORIGIN.get(origin_state.upper())
    spread_commodity = COMMODITY_TO_SPREAD_NAME.get(commodity)

    if not origin or not spread_commodity:
        return []

    if commodity == "wheat" and origin_state.upper() in ("ND", "MN"):
        spread_commodity = "HRS Wheat"

    cutoff = (
        datetime.now(tz=timezone.utc) - timedelta(weeks=weeks)
    ).strftime("%Y-%m-%dT00:00:00")

    params = {
        "$where": (
            f"date >= '{cutoff}' "
            f"AND UPPER(origin) = UPPER('{origin}') "
            f"AND UPPER(commodity) = UPPER('{spread_commodity}')"
        ),
        "$order": "date DESC",
        "$limit": "20",
    }

    cache_key = make_cache_key(f"spreads:{DATASET_PRICE_SPREADS}:{origin}:{commodity}", {})
    cached = DEFAULT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        rows = await _soda_get(DATASET_PRICE_SPREADS, params)
    except ConnectionError as exc:
        logger.warning("Price spreads fetch failed: %s", str(exc))
        return []

    if rows:
        logger.info("Spreads columns: %s", list(rows[0].keys()))
        DEFAULT_CACHE.set(cache_key, rows, TRANSPORT_TTL)
    return rows


# ---------------------------------------------------------------------------
# Public API — unified fetch for transport.py
# ---------------------------------------------------------------------------

async def fetch_transport_rates(
    origin_state: str,
    commodity: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch transport rates from all AgTransport datasets for *origin_state*.
    Returns normalised list with fields:
      mode, origin_region, destination, rate_per_bushel, note, source_dataset
    """
    commodity = _safe_commodity(commodity)
    results: List[Dict[str, Any]] = []

    # ── 1. Barge spot rates ─────────────────────────────────────────────────
    try:
        barge_rows = await fetch_barge_rates(origin_state, commodity, weeks=4)
        if barge_rows:
            latest   = barge_rows[0]
            segment  = latest.get("segment", "Unknown")
            date_lbl = str(latest.get("date", ""))[:10]
            results.append({
                "mode":            "barge",
                "origin_region":   origin_state,
                "destination":     f"Gulf Export via {segment}",
                "rate_per_bushel": latest.get("rate_per_bushel"),
                "note": (
                    f"Barge spot {latest.get('rate_pct', '?')}% of tariff "
                    f"(week of {date_lbl})"
                ),
                "source_dataset":  DATASET_BARGE_SPOT,
            })
    except ConnectionError as exc:
        logger.warning("Barge rates skipped for %s: %s", origin_state, str(exc))

    # ── 2. Truck rates ──────────────────────────────────────────────────────
    try:
        truck_rows = await fetch_truck_rates(origin_state)
        if truck_rows:
            latest        = truck_rows[0]
            rate, col     = _extract_truck_rate(latest)
            date_lbl      = str(latest.get("date", ""))[:10]
            results.append({
                "mode":            "truck",
                "origin_region":   origin_state,
                "destination":     "Regional Elevator / Local Market",
                "rate_per_bushel": rate,
                "note": (
                    f"Quarterly truck rate ({latest.get('region', '')} region, "
                    f"Q{latest.get('quarter', '?')} {latest.get('year', '')}): "
                    f"{col}=${rate}/bu"
                ),
                "source_dataset":  DATASET_TRUCK_RATES,
            })
    except ConnectionError as exc:
        logger.warning("Truck rates skipped for %s: %s", origin_state, str(exc))

    # ── 3. Price spreads (implied transport cost) ───────────────────────────
    try:
        spread_rows = await fetch_price_spreads(commodity, origin_state, weeks=4)
        if spread_rows:
            most_recent = spread_rows[0].get("date", "")
            for row in spread_rows:
                if row.get("date") != most_recent:
                    break
                spread      = _first(row, "spread", "price_spread")
                destination = _first(row, "destination", "dest", default="Export Terminal")
                origin_px   = _first(row, "origin_price", "origin_px")
                dest_px     = _first(row, "destination_price", "dest_price", "destination_px")
                try:
                    rate = abs(float(spread)) if spread is not None else None
                except (TypeError, ValueError):
                    rate = None
                results.append({
                    "mode":            "rail_or_barge",
                    "origin_region":   origin_state,
                    "destination":     str(destination),
                    "rate_per_bushel": rate,
                    "note": (
                        f"Implied transport: origin ${origin_px}/bu → "
                        f"{destination} ${dest_px}/bu "
                        f"(week of {str(most_recent)[:10]})"
                    ),
                    "source_dataset":  DATASET_PRICE_SPREADS,
                })
    except Exception as exc:
        logger.warning("Price spreads skipped for %s: %s", origin_state, str(exc))

    # ── 4. Cost indicators (context / no direct $/bu) ──────────────────────
    try:
        indicators = await fetch_cost_indicators(weeks=2)
        if indicators:
            latest   = indicators[0]
            date_lbl = str(latest.get("date", ""))[:10]

            truck_idx  = _first(latest, *_INDICATOR_CANDIDATES["truck"])
            barge_idx  = _first(latest, *_INDICATOR_CANDIDATES["barge"])
            diesel     = _first(latest, *_INDICATOR_CANDIDATES["diesel"])
            railcar    = _first(latest, *_INDICATOR_CANDIDATES["railcar_bid"])

            results.append({
                "mode":            "rail",
                "origin_region":   origin_state,
                "destination":     "Export Terminal (rail index)",
                "rate_per_bushel": None,
                "note": (
                    f"Cost indices week of {date_lbl}: "
                    f"truck={truck_idx}, barge={barge_idx}; "
                    f"diesel=${diesel}/gal, railcar bid=${railcar}/car"
                ),
                "source_dataset":  DATASET_COST_INDICATORS,
            })
    except Exception as exc:
        logger.warning("Cost indicators skipped: %s", str(exc))

    if not results:
        logger.warning(
            "No Socrata transport data for state=%s commodity=%s — using fallback.",
            origin_state, commodity,
        )

    return results


def fallback_transport() -> List[Dict[str, Any]]:
    return [dict(e) for e in SAMPLE_TRANSPORT]