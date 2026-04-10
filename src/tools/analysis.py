import asyncio
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from clients import ams_client
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


def _build_selling_options(prices: List[dict], transport: List[dict], origin_state: str) -> List[SellingOption]:
    if not prices or not transport:
        return []
    filtered_transport = [rate for rate in transport if rate.get("origin_region") == origin_state]
    if not filtered_transport:
        filtered_transport = transport

    seen = set()
    options: List[SellingOption] = []
    for price in prices:
        market = price.get("location_name", "Unknown")
        cash_price = float(price.get("cash_price", 0.0))
        for rate in filtered_transport:
            mode = rate.get("mode", "")
            destination = rate.get("destination", "")
            key = (market, mode)
            if key in seen:
                continue
            seen.add(key)
            transport_cost = float(rate.get("rate_per_bushel", 0.0))
            net_price = cash_price - transport_cost
            options.append(
                SellingOption(
                    market=market,
                    mode=mode,
                    cash_price=cash_price,
                    transport_cost=transport_cost,
                    net_price=net_price,
                    destination=destination,
                )
            )
    return options


async def _fetch_inputs(commodity: str, origin_state: str, span=None) -> Tuple[List[dict], List[dict], bool]:
    sample = False
    try:
        if span:
            with span.child_span("prices_and_transport"):
                prices, transport = await asyncio.gather(
                    ams_client.fetch_grain_prices(commodity, origin_state),
                    ams_client.fetch_transport_report(),
                )
        else:
            prices, transport = await asyncio.gather(
                ams_client.fetch_grain_prices(commodity, origin_state),
                ams_client.fetch_transport_report(),
            )
    except ConnectionError:
        prices = ams_client.fallback_prices()
        transport = ams_client.fallback_transport()
        sample = True

    if not prices:
        prices = ams_client.fallback_prices()
        sample = True
    if not transport:
        transport = ams_client.fallback_transport()
        sample = True

    return prices, transport, sample


async def rank_selling_options(commodity: str, farm_location: str, radius_miles: Optional[int] = None, span=None) -> str:
    origin_state = resolve_location(farm_location)
    prices, transport, sample = await _fetch_inputs(commodity, origin_state, span)

    options = _build_selling_options(prices, transport, origin_state)
    if not options:
        return "No selling options available for the provided inputs."

    options = sorted(options, key=lambda o: o.net_price, reverse=True)[:10]
    rows = []
    for option in options:
        rows.append(
            [
                option.market,
                option.mode,
                option.destination,
                f"{option.cash_price:.2f}",
                f"{option.transport_cost:.2f}",
                f"{option.net_price:.2f}",
            ]
        )

    table = _format_table(
        ["Market", "Mode", "Destination", "Cash", "Transport", "Net"],
        rows,
    )

    note = ""
    if sample:
        note = "\n\nNote: Sample data in use because live USDA data was unavailable."
    radius_note = ""
    if radius_miles:
        radius_note = f"\nRadius filter requested: {radius_miles} miles (informational only)."

    return f"Ranked selling options for {commodity} from {farm_location} ({origin_state})\n\n{table}{radius_note}{note}"


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
        rows.append(
            [
                option.market,
                option.mode,
                f"{option.net_price:.2f}",
                f"{volume_bushels}",
                f"{total_revenue:,.2f}",
            ]
        )

    table = _format_table(
        ["Market", "Mode", "Net/BU", "Bushels", "Total Revenue"],
        rows,
    )

    note = ""
    if sample:
        note = "\n\nNote: Sample data in use because live USDA data was unavailable."

    return (
        f"Profit simulation for {commodity} from {farm_location} ({origin_state})\n"
        f"Volume: {volume_bushels} bushels\n\n{table}{note}"
    )
