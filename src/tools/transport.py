from typing import Any, List, Optional

from clients import transport_client
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
    separator   = "-+-".join("-" * widths[i] for i in range(len(headers)))
    lines.append(header_line)
    lines.append(separator)
    for row in rows:
        line = " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))
        lines.append(line)
    return "\n".join(lines)


def _format_rate(rate: Any) -> str:
    """Format rate_per_bushel — show 'N/A (index)' when None."""
    if rate is None:
        return "N/A (index)"
    try:
        return f"{float(rate):.2f}"
    except (TypeError, ValueError):
        return str(rate)


async def get_transportation_costs(
    farm_location: str,
    commodity: Optional[str] = None,
    mode: Optional[str] = None,
    span=None,
) -> str:
    origin_state, _, _ = resolve_location(farm_location)
    # Guard both optional params against None before any .lower() call
    commodity = (commodity or "corn").lower()
    sample = False

    try:
        if span:
            with span.child_span("socrata_transport_fetch"):
                rates = await transport_client.fetch_transport_rates(
                    origin_state=origin_state,
                    commodity=commodity,
                )
        else:
            rates = await transport_client.fetch_transport_rates(
                origin_state=origin_state,
                commodity=commodity,
            )
    except Exception as exc:
        logger.warning("Transport fetch failed for %s: %s", origin_state, str(exc))
        rates = transport_client.fallback_transport()
        sample = True

    if not rates:
        rates = transport_client.fallback_transport()
        sample = True

    # Mode filter — guard against None values in the entry's mode field
    if mode:
        mode_lower = mode.lower()
        filtered = [
            r for r in rates
            if (r.get("mode") or "").lower() == mode_lower
        ]
        if not filtered:
            logger.debug(
                "Mode filter '%s' matched no rates for %s — showing all modes.",
                mode, origin_state,
            )
            filtered = rates
    else:
        filtered = rates

    rows = []
    for entry in filtered[:12]:
        rows.append([
            entry.get("mode", ""),
            entry.get("origin_region", origin_state),
            entry.get("destination", ""),
            _format_rate(entry.get("rate_per_bushel")),
            entry.get("note", ""),
        ])

    table = _format_table(
        ["Mode", "Origin", "Destination", "Rate/BU", "Note"],
        rows,
    )

    notes: List[str] = []
    if sample or any("[FALLBACK]" in str(r.get("note", "")) for r in filtered):
        notes.append(
            "Note: Fallback sample data in use — live AgTransport data was unavailable."
        )
    if any(r.get("rate_per_bushel") is None for r in filtered):
        notes.append(
            "Note: Rows showing 'N/A (index)' are cost index values with no direct "
            "$/bu rate. Use barge and truck rows for selling decisions."
        )

    note_block = ("\n\n" + "\n".join(notes)) if notes else ""

    return (
        f"Transportation costs from {farm_location} ({origin_state}) "
        f"for {commodity}\n\n{table}{note_block}"
    )