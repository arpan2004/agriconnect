import asyncio
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from cache import DEFAULT_CACHE, make_cache_key
from observability import get_logger
from security import redact_secrets

BASE_URL = "https://quickstats.nass.usda.gov/api/api_GET/"
ALLOWED_DOMAINS = {"quickstats.nass.usda.gov"}

NASS_DAILY_LIMIT = int(os.getenv("NASS_DAILY_LIMIT", "50"))
NASS_TTL = 60 * 60

logger = get_logger("agriconnect.nass")

# -----------------------
# RATE LIMIT TRACKING
# -----------------------
_nass_request_log: List[float] = []


def _is_allowed(url: str) -> bool:
    return urlparse(url).netloc in ALLOWED_DOMAINS


def _check_nass_rate_limit() -> bool:
    now = time.time()
    cutoff = now - 86400

    while _nass_request_log and _nass_request_log[0] < cutoff:
        _nass_request_log.pop(0)

    if len(_nass_request_log) >= NASS_DAILY_LIMIT:
        return False

    _nass_request_log.append(now)
    return True


# -----------------------
# HTTP FETCH
# -----------------------
async def _fetch_with_retry(params: Dict[str, Any]) -> httpx.Response:
    if not _is_allowed(BASE_URL):
        raise ConnectionError("Blocked by URL allowlist.")

    last_error: Exception = ConnectionError("Unknown error")

    for attempt in range(1, 3):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(BASE_URL, params=params)

            if response.status_code == 429:
                await asyncio.sleep(1.5 ** attempt)
                continue

            if response.status_code == 400:
                raise ValueError("Bad NASS query (400)")

            if 400 <= response.status_code < 500:
                raise ConnectionError(f"HTTP {response.status_code}")

            if response.status_code >= 500:
                raise httpx.HTTPStatusError("Server error", request=response.request, response=response)

            return response

        except Exception as exc:
            last_error = exc
            if attempt >= 2:
                break
            await asyncio.sleep(1.5 ** attempt)

    raise ConnectionError(str(last_error))


# -----------------------
# CORE FETCH
# -----------------------
async def _fetch_quickstats_rows(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not _check_nass_rate_limit():
        return []

    api_key = os.getenv("USDA_NASS_API_KEY", "DEMO_KEY")

    request_params = {
        "key": api_key,
        "format": "JSON",
        **{k: v for k, v in params.items() if v not in (None, "")},
    }

    cache_key = make_cache_key(BASE_URL, request_params)
    cached = DEFAULT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        response = await _fetch_with_retry(request_params)
        payload = response.json()

        results = payload.get("data") or payload.get("results") or []
        if not isinstance(results, list):
            results = []

        DEFAULT_CACHE.set(cache_key, results, NASS_TTL)
        return results

    except Exception as exc:
        logger.warning(redact_secrets(str(exc)))
        return []  # <-- important: don't crash upstream


# -----------------------
# NORMALIZATION
# -----------------------
def _normalize_fundamental_row(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "year": str(item.get("year") or ""),
        "Value": item.get("Value") or "",
        "commodity_desc": item.get("commodity_desc") or "",
        "state_alpha": item.get("state_alpha") or "",
        "unit_desc": item.get("unit_desc") or "",
        "short_desc": item.get("short_desc") or "",
        "source_desc": item.get("source_desc") or "USDA NASS",
    }


# -----------------------
# QUERY CONFIG
# -----------------------
FUNDAMENTAL_QUERIES = {
    "corn": {
        "planted_acres": {
            "short_descs": [
                "CORN - ACRES PLANTED",
                "CORN, GRAIN - ACRES PLANTED",
            ],
            "statisticcat_desc": "AREA PLANTED",
        },
        "yield": {
            "short_descs": [
                "CORN - YIELD, MEASURED IN BU / ACRE",
            ],
            "statisticcat_desc": "YIELD",
        },
        "production": {
            "short_descs": [
                "CORN - PRODUCTION, MEASURED IN BU",
            ],
            "statisticcat_desc": "PRODUCTION",
        },
    }
}


# -----------------------
# SMART QUERY ENGINE
# -----------------------
async def _fetch_fundamental_metric(
    commodity: str,
    state: str,
    year: int,
    metric: str,
) -> Optional[Dict[str, Any]]:
    config = FUNDAMENTAL_QUERIES.get(commodity.lower(), {}).get(metric)
    if not config:
        return None

    base = {
        "agg_level_desc": "STATE",
        "state_alpha": state,
        "year__GE": str(year),  # ✅ FIXED
        "domain_desc": "TOTAL",
        "freq_desc": "ANNUAL",
        "source_desc": "SURVEY",
    }

    # -----------------------
    # 1. Try exact short_desc
    # -----------------------
    for desc in config.get("short_descs", []):
        rows = await _fetch_quickstats_rows({
            **base,
            "short_desc": desc,
        })
        if rows:
            return _normalize_fundamental_row(rows[0])

    # -----------------------
    # 2. Fallback: looser query
    # -----------------------
    rows = await _fetch_quickstats_rows({
        **base,
        "commodity_desc": commodity.upper(),
        "statisticcat_desc": config.get("statisticcat_desc"),
    })
    if rows:
        return _normalize_fundamental_row(rows[0])

    # -----------------------
    # 3. Last fallback: even looser
    # -----------------------
    rows = await _fetch_quickstats_rows({
        "commodity_desc": commodity.upper(),
        "state_alpha": state,
        "year__GE": str(year),
    })
    if rows:
        return _normalize_fundamental_row(rows[0])

    return None


# -----------------------
# PUBLIC API
# -----------------------
async def fetch_crop_fundamentals(
    commodity: str,
    state: str,
    year: int,
) -> Dict[str, Dict[str, Any]]:
    snapshot: Dict[str, Dict[str, Any]] = {}

    for metric in ("planted_acres", "yield", "production"):
        row = await _fetch_fundamental_metric(commodity, state, year, metric)
        if row:
            snapshot[metric] = row

    return snapshot