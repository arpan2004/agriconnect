from datetime import datetime
from typing import Any, Dict, List, Optional

from clients import nass_client
from observability import get_logger
from utils.geo import resolve_location

logger = get_logger("agriconnect.tools.fundamentals")

METRIC_ORDER = [
    ("planted_acres", "Planted Acres"),
    ("yield", "Yield"),
    ("production", "Production"),
]


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


def _is_missing(value: Any) -> bool:
    text = str(value).strip()
    return text in {"", "(D)", "(Z)", "(NA)"}


async def get_crop_fundamentals(
    commodity: str,
    location: str,
    year: Optional[int] = None,
    span=None,
) -> str:
    state = resolve_location(location)
    report_year = year if year is not None else datetime.now().year - 1
    sample = False

    try:
        if span:
            with span.child_span("nass_fundamentals_fetch"):
                snapshot = await nass_client.fetch_crop_fundamentals(
                    commodity,
                    state,
                    report_year,
                )
        else:
            snapshot = await nass_client.fetch_crop_fundamentals(
                commodity,
                state,
                report_year,
            )
    except ConnectionError:
        snapshot = nass_client.fallback_crop_fundamentals(commodity, state, report_year)
        sample = True

    if not snapshot:
        snapshot = nass_client.fallback_crop_fundamentals(commodity, state, report_year)
        sample = True

    rows: List[List[Any]] = []
    available_metrics: List[str] = []
    years_seen = set()
    for metric, label in METRIC_ORDER:
        entry = snapshot.get(metric)
        if not entry:
            rows.append([label, "N/A", "", str(report_year), "Unavailable"])
            continue

        value = entry.get("Value", "")
        unit = entry.get("unit_desc", "")
        entry_year = str(entry.get("year") or report_year)
        source = entry.get("source_desc", "USDA NASS")

        if _is_missing(value):
            rows.append([label, "N/A", unit, entry_year, source])
            continue

        rows.append([label, value, unit, entry_year, source])
        available_metrics.append(f"{label.lower()} {value} {unit}".strip())
        years_seen.add(entry_year)

    table = _format_table(
        ["Metric", "Value", "Unit", "Year", "Source"],
        rows,
    )

    summary = (
        f"Crop fundamentals for {commodity} in {location} ({state})"
        f" for {', '.join(sorted(years_seen)) or str(report_year)}."
    )
    if available_metrics:
        summary += " Snapshot: " + "; ".join(available_metrics[:3]) + "."

    note = ""
    if sample:
        note = "\n\nNote: Sample data in use because live NASS acreage/yield/production data was unavailable."

    return f"{summary}\n\n{table}{note}"
