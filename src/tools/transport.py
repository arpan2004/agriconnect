from typing import Any, List, Optional

from clients import ams_client
from observability import get_logger
from utils.geo import resolve_location

logger = get_logger("agriconnect.tools.transport")


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


async def get_transportation_costs(farm_location: str, mode: Optional[str] = None, span=None) -> str:
    origin_state = resolve_location(farm_location)
    sample = False
    try:
        if span:
            with span.child_span("ams_transport_fetch"):
                rates = await ams_client.fetch_transport_report()
        else:
            rates = await ams_client.fetch_transport_report()
    except ConnectionError:
        rates = ams_client.fallback_transport()
        sample = True

    if not rates:
        rates = ams_client.fallback_transport()
        sample = True

    filtered = [r for r in rates if r.get("origin_region") == origin_state]
    if mode:
        filtered = [r for r in filtered if r.get("mode") == mode]
    if not filtered:
        filtered = rates

    rows = []
    for entry in filtered[:12]:
        rows.append(
            [
                entry.get("mode", ""),
                entry.get("origin_region", ""),
                entry.get("destination", ""),
                f"{entry.get('rate_per_bushel', 0.0):.2f}",
                entry.get("note", ""),
            ]
        )

    table = _format_table(["Mode", "Origin", "Destination", "Rate/BU", "Note"], rows)
    note = ""
    if sample or any("[SAMPLE]" in str(r.get("note", "")) for r in filtered):
        note = "\n\nNote: Sample data in use because live AMS transport data was unavailable."

    return f"Transportation costs from {farm_location} ({origin_state})\n\n{table}{note}"
