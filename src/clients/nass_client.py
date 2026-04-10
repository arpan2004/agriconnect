import asyncio
import os
import time
from typing import Any, Dict, List
from urllib.parse import urlparse

import httpx

from cache import DEFAULT_CACHE, make_cache_key
from observability import get_logger
from security import redact_secrets

BASE_URL = "https://quickstats.nass.usda.gov/api/api_GET/"
ALLOWED_DOMAINS = {"quickstats.nass.usda.gov"}

NASS_DAILY_LIMIT = int(os.getenv("NASS_DAILY_LIMIT", "50"))
NASS_TTL = 60 * 60

NASS_SAMPLE = [
    {
        "week_ending": "2026-02-02",
        "Value": "4.52",
        "commodity_desc": "CORN",
        "state_alpha": "IA",
        "unit_desc": "$ / BU",
        "source_desc": "USDA NASS [SAMPLE]",
    },
    {
        "week_ending": "2026-02-09",
        "Value": "4.58",
        "commodity_desc": "CORN",
        "state_alpha": "IA",
        "unit_desc": "$ / BU",
        "source_desc": "USDA NASS [SAMPLE]",
    },
    {
        "week_ending": "2026-02-16",
        "Value": "4.61",
        "commodity_desc": "CORN",
        "state_alpha": "IA",
        "unit_desc": "$ / BU",
        "source_desc": "USDA NASS [SAMPLE]",
    },
    {
        "week_ending": "2026-02-23",
        "Value": "4.67",
        "commodity_desc": "CORN",
        "state_alpha": "IA",
        "unit_desc": "$ / BU",
        "source_desc": "USDA NASS [SAMPLE]",
    },
    {
        "week_ending": "2026-03-01",
        "Value": "4.65",
        "commodity_desc": "CORN",
        "state_alpha": "IA",
        "unit_desc": "$ / BU",
        "source_desc": "USDA NASS [SAMPLE]",
    },
]

_nass_request_log: List[float] = []

logger = get_logger("agriconnect.nass")


def _is_allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc in ALLOWED_DOMAINS


def _check_nass_rate_limit() -> bool:
    now = time.time()
    cutoff = now - 24 * 60 * 60
    while _nass_request_log and _nass_request_log[0] < cutoff:
        _nass_request_log.pop(0)
    if len(_nass_request_log) >= NASS_DAILY_LIMIT:
        return False
    _nass_request_log.append(now)
    return True


async def _fetch_with_retry(params: Dict[str, Any]) -> httpx.Response:
    if not _is_allowed(BASE_URL):
        raise ConnectionError("Blocked by URL allowlist.")

    last_error: Exception = ConnectionError("Unknown error")
    for attempt in range(1, 3):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(BASE_URL, params=params)

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                sleep_for = float(retry_after) if retry_after else 1.5 ** attempt
                await asyncio.sleep(sleep_for)
                continue

            if 400 <= response.status_code < 500:
                raise ConnectionError(f"HTTP {response.status_code}")

            if response.status_code >= 500:
                raise httpx.HTTPStatusError("Server error", request=response.request, response=response)

            if len(response.content) > 5 * 1024 * 1024:
                raise ConnectionError("Response too large")

            return response
        except (httpx.TimeoutException, httpx.HTTPError, ConnectionError) as exc:
            last_error = exc
            if attempt >= 2:
                break
            await asyncio.sleep(1.5 ** attempt)

    raise ConnectionError(str(last_error))


def _normalize_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in results:
        normalized.append(
            {
                "week_ending": item.get("week_ending") or item.get("week_ending_date") or "",
                "Value": item.get("Value") or item.get("value") or "",
                "commodity_desc": item.get("commodity_desc") or item.get("commodity") or "",
                "state_alpha": item.get("state_alpha") or item.get("state") or "",
                "unit_desc": item.get("unit_desc") or "",
                "source_desc": item.get("source_desc") or "USDA NASS",
            }
        )
    return normalized


async def fetch_commodity_prices(commodity: str, state: str) -> List[Dict[str, Any]]:
    if not _check_nass_rate_limit():
        return []

    api_key = os.getenv("USDA_NASS_API_KEY", "DEMO_KEY")
    params = {
        "key": api_key,
        "commodity_desc": commodity.upper(),
        "statisticcat_desc": "PRICE RECEIVED",
        "unit_desc": "$ / BU",
        "agg_level_desc": "STATE",
        "state_alpha": state,
        "freq_desc": "WEEKLY",
        "format": "JSON",
    }

    cache_key = make_cache_key(BASE_URL, params)
    cached = DEFAULT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        response = await _fetch_with_retry(params)
        payload = response.json()
        results = payload.get("data") or payload.get("results") or []
        normalized = _normalize_results(results)
        DEFAULT_CACHE.set(cache_key, normalized, NASS_TTL)
        return normalized
    except Exception as exc:
        logger.warning(redact_secrets(str(exc)))
        raise ConnectionError("NASS fetch failed") from exc


def fallback_prices() -> List[Dict[str, Any]]:
    return [dict(entry) for entry in NASS_SAMPLE]
