from typing import Any, List, Optional, Tuple

from clients import nass_client
from observability import get_logger
from utils.geo import resolve_location

logger = get_logger("agriconnect.tools.trends")


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


def _extract_series(data: List[dict]) -> List[Tuple[str, float]]:
    series: List[Tuple[str, float]] = []
    for entry in data:
        week = entry.get("week_ending", "")
        value = entry.get("Value", "")
        try:
            price = float(str(value).replace(",", ""))
        except ValueError:
            continue
        series.append((week, price))
    series.sort(key=lambda item: item[0])
    return series


def _classify_change(net_change: float) -> Tuple[str, str]:
    if abs(net_change) < 0.02:
        return "flat", "slight"
    direction = "rising" if net_change > 0 else "falling"
    magnitude = "slight"
    if abs(net_change) >= 0.30:
        magnitude = "sharp"
    elif abs(net_change) >= 0.10:
        magnitude = "moderate"
    return direction, magnitude


async def get_market_trends(commodity: str, location: str, span=None) -> str:
    state = resolve_location(location)
    sample = False
    try:
        if span:
            with span.child_span("nass_trends_fetch"):
                data = await nass_client.fetch_commodity_prices(commodity, state)
        else:
            data = await nass_client.fetch_commodity_prices(commodity, state)
    except ConnectionError:
        data = nass_client.fallback_prices()
        sample = True

    if not data:
        data = nass_client.fallback_prices()
        sample = True

    series = _extract_series(data)
    if not series:
        return "No market trend data available."

    rows = []
    changes = []
    prev_price: Optional[float] = None
    for week, price in series:
        change = 0.0
        if prev_price is not None:
            change = price - prev_price
            changes.append(change)
        prev_price = price
        change_str = f"{change:+.2f}" if change else "0.00"
        rows.append([week, f"{price:.2f}", change_str])

    net_change = series[-1][1] - series[0][1]
    avg_move = sum(abs(c) for c in changes) / len(changes) if changes else 0.0
    price_range = max(p for _, p in series) - min(p for _, p in series)

    table = _format_table(["Week Ending", "Price", "Change"], rows)
    summary = (
        f"Net change: {net_change:+.2f} | Avg weekly move: {avg_move:.2f} | "
        f"Volatility range: {price_range:.2f}"
    )
    note = ""
    if sample:
        note = "\n\nNote: Sample data in use because live NASS data was unavailable."

    return (
        f"Market trends for {commodity} in {location} ({state})\n\n{table}\n\n{summary}{note}"
    )


async def get_weekly_summary(commodity: str, location: str, span=None) -> str:
    state = resolve_location(location)
    sample = False
    try:
        if span:
            with span.child_span("nass_summary_fetch"):
                data = await nass_client.fetch_commodity_prices(commodity, state)
        else:
            data = await nass_client.fetch_commodity_prices(commodity, state)
    except ConnectionError:
        data = nass_client.fallback_prices()
        sample = True

    if not data:
        data = nass_client.fallback_prices()
        sample = True

    series = _extract_series(data)
    if not series:
        return "No weekly summary available."

    net_change = series[-1][1] - series[0][1]
    direction, magnitude = _classify_change(net_change)

    recommendation = "Hold and monitor the market."
    if direction == "rising" and magnitude in {"moderate", "sharp"}:
        recommendation = "Consider pricing a portion of the crop to lock in gains."
    elif direction == "falling" and magnitude in {"moderate", "sharp"}:
        recommendation = "Consider defensive hedging or waiting for stabilization."

    summary = (
        f"Weekly summary for {commodity} in {location} ({state}): Prices are {direction} "
        f"with a {magnitude} net move of {net_change:+.2f} per bushel over the period. "
        f"Recommendation: {recommendation}"
    )
    if sample:
        summary += " Note: Sample data in use because live NASS data was unavailable."

    return summary
