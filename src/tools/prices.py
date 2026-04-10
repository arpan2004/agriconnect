from typing import Any, List, Optional

from clients import ams_client
from observability import get_logger
from utils.geo import resolve_location

logger = get_logger("agriconnect.tools.prices")


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


async def get_cash_prices(commodity: str, location: str, radius_miles: Optional[int] = None, span=None) -> str:
    state = resolve_location(location)
    sample = False
    try:
        if span:
            with span.child_span("ams_prices_fetch"):
                prices = await ams_client.fetch_grain_prices(commodity, state)
        else:
            prices = await ams_client.fetch_grain_prices(commodity, state)
    except ConnectionError:
        prices = ams_client.fallback_prices()
        sample = True

    if not prices:
        prices = ams_client.fallback_prices()
        sample = True

    prices = sorted(prices, key=lambda p: p.get("cash_price", 0.0), reverse=True)[:10]

    rows = []
    for entry in prices:
        rows.append(
            [
                entry.get("location_name", ""),
                entry.get("state", ""),
                entry.get("market_type", ""),
                f"{entry.get('cash_price', 0.0):.2f}",
                f"{entry.get('basis', 0.0):+.2f}",
                entry.get("report_date", ""),
            ]
        )

    table = _format_table(
        ["Location", "State", "Type", "Cash", "Basis", "Report Date"],
        rows,
    )
    note = ""
    if sample or any("[SAMPLE]" in str(p.get("data_source", "")) for p in prices):
        note = "\n\nNote: Sample data in use because live AMS data was unavailable."
    radius_note = ""
    if radius_miles:
        radius_note = f"\nRadius filter requested: {radius_miles} miles (informational only)."

    return f"Cash prices for {commodity} near {location} ({state})\n\n{table}{radius_note}{note}"
