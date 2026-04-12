import asyncio
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from clients import ams_client, transport_client
from observability import get_logger
from utils.geo import resolve_location

logger = get_logger("agriconnect.tools.analysis")


@dataclass
class SellingOption:
    market: str
    mode: str
    cash_price: float
    transport_cost: float
    net_price: float
    destination: str


def _format_table(headers: List[str], rows: List[List[Any]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(str(cell)))
    lines = []
    header_line = " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    separator = "-+-".join("-" * widths[i] for i in range(len(headers)))
    lines.append(header_line)
    lines.append(separator)
    for row in rows:
        line = " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))
        lines.append(line)
    return "\n".join(lines)


def _build_selling_options(
    prices: List[dict],
    transport: List[dict],
    origin_state: str,
) -> List[SellingOption]:
    if not prices or not transport:
        return []

    # Prefer rates that match the origin state; fall back to all rates
    filtered_transport = [r for r in transport if r.get("origin_region") == origin_state]
    if not filtered_transport:
        filtered_transport = transport

    # Skip transport rows with no usable rate (cost-index-only rows)
    usable_transport = [
        r for r in filtered_transport
        if r.get("rate_per_bushel") is not None
    ]
    if not usable_transport:
        logger.warning(
            "All transport rows for %s have rate_per_bushel=None (index-only). "
            "Falling back to all transport rows with non-None rates.",
            origin_state,
        )
        usable_transport = [r for r in transport if r.get("rate_per_bushel") is not None]

    if not usable_transport:
        logger.warning("No transport rows with a usable rate_per_bushel — cannot build options.")
        return []

    seen: set = set()
    options: List[SellingOption] = []
    for price in prices:
        market     = price.get("location_name", "Unknown")
        cash_price = float(price.get("cash_price", 0.0))
        for rate in usable_transport:
            mode        = rate.get("mode", "")
            destination = rate.get("destination", "")
            key = (market, mode)
            if key in seen:
                continue
            seen.add(key)
            transport_cost = float(rate.get("rate_per_bushel", 0.0))
            options.append(
                SellingOption(
                    market=market,
                    mode=mode,
                    cash_price=cash_price,
                    transport_cost=transport_cost,
                    net_price=cash_price - transport_cost,
                    destination=destination,
                )
            )
    return options


async def _fetch_inputs(
    commodity: str,
    origin_state: str,
    span=None,
) -> Tuple[List[dict], List[dict], bool]:
    """
    Fetch grain prices from AMS and transport rates from AgTransport (Socrata).
    Each source falls back independently so a failure in one doesn't
    force sample data for the other.
    """
    sample = False

    # ── Prices (AMS MyMarketNews) ────────────────────────────────────────────
    prices: List[dict] = []
    try:
        if span:
            with span.child_span("ams_prices"):
                prices = await ams_client.fetch_grain_prices(commodity, origin_state)
        else:
            prices = await ams_client.fetch_grain_prices(commodity, origin_state)
    except ConnectionError as exc:
        logger.warning("AMS price fetch failed (%s/%s): %s", commodity, origin_state, str(exc))

    if not prices:
        logger.info("Using AMS sample prices for %s/%s.", commodity, origin_state)
        prices = ams_client.fallback_prices()
        sample = True

    # ── Transport (USDA AgTransport / Socrata) ───────────────────────────────
    transport: List[dict] = []
    try:
        if span:
            with span.child_span("socrata_transport"):
                transport = await transport_client.fetch_transport_rates(
                    origin_state=origin_state,
                    commodity=commodity,
                )
        else:
            transport = await transport_client.fetch_transport_rates(
                origin_state=origin_state,
                commodity=commodity,
            )
    except Exception as exc:
        logger.warning("Transport fetch failed (%s): %s", origin_state, str(exc))

    if not transport:
        logger.info("Using sample transport rates for %s.", origin_state)
        transport = transport_client.fallback_transport()
        sample = True

    return prices, transport, sample


async def rank_selling_options(
    commodity: str,
    farm_location: str,
    radius_miles: Optional[int] = None,
    span=None,
) -> str:
    origin_state = resolve_location(farm_location)
    prices, transport, sample = await _fetch_inputs(commodity, origin_state, span)

    options = _build_selling_options(prices, transport, origin_state)
    if not options:
        return "No selling options available for the provided inputs."

    options = sorted(options, key=lambda o: o.net_price, reverse=True)[:10]

    rows = []
    for option in options:
        rows.append([
            option.market,
            option.mode,
            option.destination,
            f"{option.cash_price:.2f}",
            f"{option.transport_cost:.2f}",
            f"{option.net_price:.2f}",
        ])

    table = _format_table(
        ["Market", "Mode", "Destination", "Cash", "Transport", "Net"],
        rows,
    )

    notes: List[str] = []
    if sample:
        notes.append("Note: Sample data in use because live USDA data was unavailable.")
    if radius_miles:
        notes.append(f"Radius filter requested: {radius_miles} miles (informational only).")
    note_block = ("\n\n" + "\n".join(notes)) if notes else ""

    return (
        f"Ranked selling options for {commodity} from {farm_location} ({origin_state})"
        f"\n\n{table}{note_block}"
    )


async def simulate_profit(
    commodity: str,
    farm_location: str,
    volume_bushels: int,
    top_n: int = 5,
    span=None,
) -> str:
    origin_state = resolve_location(farm_location)
    prices, transport, sample = await _fetch_inputs(commodity, origin_state, span)

    options = _build_selling_options(prices, transport, origin_state)
    if not options:
        return "No profit simulation available for the provided inputs."

    options = sorted(options, key=lambda o: o.net_price, reverse=True)[:top_n]

    rows = []
    for option in options:
        total_revenue = option.net_price * volume_bushels
        rows.append([
            option.market,
            option.mode,
            f"{option.net_price:.2f}",
            f"{volume_bushels:,}",
            f"{total_revenue:,.2f}",
        ])

    table = _format_table(
        ["Market", "Mode", "Net/BU", "Bushels", "Total Revenue"],
        rows,
    )

    note_block = "\n\nNote: Sample data in use because live USDA data was unavailable." if sample else ""

    return (
        f"Profit simulation for {commodity} from {farm_location} ({origin_state})\n"
        f"Volume: {volume_bushels:,} bushels\n\n{table}{note_block}"
    )