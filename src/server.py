import asyncio
import json
from typing import Any, Dict

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool,
        TextContent,
        Resource,
        Prompt,
        PromptArgument,
        PromptMessage,
        GetPromptResult,
    )
    from mcp.server.lowlevel.helper_types import ReadResourceContents
except Exception:  # pragma: no cover - fallback for environments without MCP
    from dataclasses import dataclass

    @dataclass
    class Tool:
        name: str
        description: str
        inputSchema: Dict[str, Any]

    @dataclass
    class TextContent:
        type: str
        text: str

    @dataclass
    class Resource:
        uri: str
        name: str
        description: str
        mimeType: str = "application/json"

    @dataclass
    class PromptArgument:
        name: str
        description: str
        required: bool = False

    @dataclass
    class Prompt:
        name: str
        description: str
        arguments: list

    @dataclass
    class PromptMessage:
        role: str
        content: TextContent

    @dataclass
    class GetPromptResult:
        messages: list
        description: str | None = None

    @dataclass
    class ReadResourceContents:
        content: str | bytes
        mime_type: str | None = None
        meta: dict[str, Any] | None = None

    class Server:  # minimal stub
        def __init__(self, name: str) -> None:
            self.name = name

        def list_tools(self):
            def decorator(fn):
                return fn
            return decorator

        def call_tool(self):
            def decorator(fn):
                return fn
            return decorator

        def list_resources(self):
            def decorator(fn):
                return fn
            return decorator

        def read_resource(self):
            def decorator(fn):
                return fn
            return decorator

        def list_prompts(self):
            def decorator(fn):
                return fn
            return decorator

        def get_prompt(self):
            def decorator(fn):
                return fn
            return decorator

        async def run(self, *_args, **_kwargs):
            return None

        def create_initialization_options(self):
            return {}

    async def stdio_server():
        raise RuntimeError("MCP stdio server is unavailable")

from cache import DEFAULT_CACHE
from observability import get_logger, log_audit_event, trace_span
from security import rate_limiter, sanitize_input, sanitize_output, validate_tool_args
from tools.analysis import rank_selling_options, simulate_profit
from tools.fundamentals import get_crop_fundamentals
from tools.prices import get_cash_prices
from tools.transport import get_transportation_costs
from tools.trends import get_market_trends, get_weekly_summary

logger = get_logger("agriconnect.server")
server = Server("agriconnect-mcp")
SERVER_VERSION = "1.0.0"
logger.info("Starting AgriConnect MCP v1.0.0 from src/server.py")

SUPPORTED_COMMODITIES = [
    {"name": "corn", "usda_code": "CORN"},
    {"name": "soybeans", "usda_code": "SOYBEANS"},
    {"name": "wheat", "usda_code": "WHEAT"},
    {"name": "oats", "usda_code": "OATS"},
    {"name": "sorghum", "usda_code": "SORGHUM"},
]

MARKET_REGIONS = {
    "Midwest": ["Chicago IL", "Des Moines IA", "Omaha NE"],
    "Plains": ["Wichita KS", "Sioux Falls SD"],
    "Gulf": ["Gulf Export", "New Orleans LA"],
    "River": ["St. Louis MO"],
}

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "get_cash_prices": {
        "type": "object",
        "properties": {
            "commodity": {
                "type": "string",
                "enum": [c["name"] for c in SUPPORTED_COMMODITIES],
                "description": "Commodity name",
            },
            "location": {
                "type": "string",
                "description": "Farm location, e.g. Ames, IA",
                "maxLength": 200,
            },
            "radius_miles": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Search radius in miles",
            },
        },
        "required": ["commodity", "location"],
        "additionalProperties": False,
    },
    "get_transportation_costs": {
        "type": "object",
        "properties": {
            "farm_location": {
                "type": "string",
                "description": "Origin farm location",
                "maxLength": 200,
            },
            "commodity": {
                "type": "string",
                "enum": [c["name"] for c in SUPPORTED_COMMODITIES],
                "description": "Commodity name",
            },
            "mode": {
                "type": "string",
                "enum": ["truck", "rail", "barge"],
                "description": "Filter by mode",
            },
        },
        "required": ["farm_location"],
        "additionalProperties": False,
    },
    "rank_selling_options": {
        "type": "object",
        "properties": {
            "commodity": {
                "type": "string",
                "enum": [c["name"] for c in SUPPORTED_COMMODITIES],
                "description": "Commodity name",
            },
            "farm_location": {
                "type": "string",
                "description": "Origin farm location",
                "maxLength": 200,
            },
            "radius_miles": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Search radius in miles",
            },
        },
        "required": ["commodity", "farm_location"],
        "additionalProperties": False,
    },
    "simulate_profit": {
        "type": "object",
        "properties": {
            "commodity": {
                "type": "string",
                "enum": [c["name"] for c in SUPPORTED_COMMODITIES],
            },
            "farm_location": {
                "type": "string",
                "maxLength": 200,
            },
            "volume_bushels": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000000,
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["commodity", "farm_location", "volume_bushels"],
        "additionalProperties": False,
    },
    "get_market_trends": {
        "type": "object",
        "properties": {
            "commodity": {
                "type": "string",
                "enum": [c["name"] for c in SUPPORTED_COMMODITIES],
            },
            "location": {
                "type": "string",
                "maxLength": 200,
            },
        },
        "required": ["commodity", "location"],
        "additionalProperties": False,
    },
    "get_weekly_summary": {
        "type": "object",
        "properties": {
            "commodity": {
                "type": "string",
                "enum": [c["name"] for c in SUPPORTED_COMMODITIES],
            },
            "location": {
                "type": "string",
                "maxLength": 200,
            },
        },
        "required": ["commodity", "location"],
        "additionalProperties": False,
    },
    "get_crop_fundamentals": {
        "type": "object",
        "properties": {
            "commodity": {
                "type": "string",
                "enum": ["corn", "soybeans"],
                "description": "Commodity name for acreage/yield/production snapshot",
            },
            "location": {
                "type": "string",
                "description": "Farm state or location, e.g. Ames, IA",
                "maxLength": 200,
            },
            "year": {
                "type": "integer",
                "minimum": 2000,
                "maximum": 2100,
                "description": "Crop year to query",
            },
        },
        "required": ["commodity", "location"],
        "additionalProperties": False,
    },
}


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="get_cash_prices",
            description="Get current cash commodity prices near a farm location.",
            inputSchema=TOOL_SCHEMAS["get_cash_prices"],
        ),
        Tool(
            name="get_transportation_costs",
            description="Get transportation cost estimates from a farm location.",
            inputSchema=TOOL_SCHEMAS["get_transportation_costs"],
        ),
        Tool(
            name="rank_selling_options",
            description="Rank selling locations by net price after transportation costs.",
            inputSchema=TOOL_SCHEMAS["rank_selling_options"],
        ),
        Tool(
            name="simulate_profit",
            description="Simulate total revenue for a crop volume using ranked selling options.",
            inputSchema=TOOL_SCHEMAS["simulate_profit"],
        ),
        Tool(
            name="get_market_trends",
            description="Get weekly price trends for a commodity and location.",
            inputSchema=TOOL_SCHEMAS["get_market_trends"],
        ),
        Tool(
            name="get_weekly_summary",
            description="Get a narrative weekly price summary for a commodity and location.",
            inputSchema=TOOL_SCHEMAS["get_weekly_summary"],
        ),
        Tool(
            name="get_crop_fundamentals",
            description="Get planted acreage, yield, and production for corn or soybeans from USDA NASS.",
            inputSchema=TOOL_SCHEMAS["get_crop_fundamentals"],
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if not rate_limiter.check():
        log_audit_event("RATE_LIMIT", {"tool": name})
        return [TextContent(type="text", text="Rate limit exceeded. Please try again shortly.")]

    args = arguments or {}
    sanitized: Dict[str, Any] = {}
    for key, value in args.items():
        sanitized[key] = sanitize_input(value)

    schema = TOOL_SCHEMAS.get(name)
    if schema:
        validate_tool_args(sanitized, schema)

    span = trace_span(name, list(sanitized.keys()))
    try:
        if name == "get_cash_prices":
            text = await get_cash_prices(
                sanitized.get("commodity"),
                sanitized.get("location"),
                sanitized.get("radius_miles"),
                span=span,
            )
        elif name == "get_transportation_costs":
            text = await get_transportation_costs(
                sanitized.get("farm_location"),
                sanitized.get("commodity"),
                sanitized.get("mode"),
                span=span,
            )
        elif name == "rank_selling_options":
            text = await rank_selling_options(
                sanitized.get("commodity"),
                sanitized.get("farm_location"),
                sanitized.get("radius_miles"),
                span=span,
            )
        elif name == "simulate_profit":
            text = await simulate_profit(
                sanitized.get("commodity"),
                sanitized.get("farm_location"),
                sanitized.get("volume_bushels"),
                sanitized.get("top_n", 5),
                span=span,
            )
        elif name == "get_market_trends":
            text = await get_market_trends(
                sanitized.get("commodity"),
                sanitized.get("location"),
                span=span,
            )
        elif name == "get_weekly_summary":
            text = await get_weekly_summary(
                sanitized.get("commodity"),
                sanitized.get("location"),
                span=span,
            )
        elif name == "get_crop_fundamentals":
            text = await get_crop_fundamentals(
                sanitized.get("commodity"),
                sanitized.get("location"),
                sanitized.get("year"),
                span=span,
            )
        else:
            raise ValueError(f"Unknown tool: {name}")

        text = sanitize_output(text)
        span.finish("success")
        log_audit_event("TOOL_SUCCESS", {"tool": name})
        return [TextContent(type="text", text=text)]
    except Exception as exc:
        span.finish("error", str(exc))
        log_audit_event("TOOL_ERROR", {"tool": name, "error": str(exc)})
        return [TextContent(type="text", text=f"Error: {exc}")]


@server.list_resources()
async def list_resources():
    return [
        Resource(
            uri="usda://commodities/supported",
            name="Supported commodities",
            description="List of supported commodities and USDA codes.",
            mimeType="application/json",
        ),
        Resource(
            uri="usda://markets/regions",
            name="USDA market regions",
            description="Major reporting regions and markets.",
            mimeType="application/json",
        ),
        Resource(
            uri="usda://status",
            name="Server status",
            description="Live server status with cache and rate limit stats.",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def read_resource(uri: str):
    logger.info(f"Resource read requested: {uri!r} ({type(uri).__name__})")
    normalized_uri = str(uri).strip().rstrip("/")
    if normalized_uri == "usda://commodities/supported":
        payload = {"commodities": SUPPORTED_COMMODITIES}
    elif normalized_uri == "usda://markets/regions":
        payload = {"regions": MARKET_REGIONS}
    elif normalized_uri == "usda://status":
        payload = {
            "version": SERVER_VERSION,
            "cache": DEFAULT_CACHE.stats(),
            "rate_limiter": rate_limiter.status(),
        }
    else:
        raise ValueError(f"Unknown resource: {normalized_uri}")

    return [
        ReadResourceContents(
            content=json.dumps(payload, ensure_ascii=True),
            mime_type="application/json",
        )
    ]


@server.list_prompts()
async def list_prompts():
    return [
        Prompt(
            name="selling_decision",
            description="Help decide where to sell a commodity based on net price.",
            arguments=[
                PromptArgument(name="commodity", description="Commodity name", required=True),
                PromptArgument(name="farm_location", description="Farm location", required=True),
                PromptArgument(name="volume_bushels", description="Bushels to sell", required=False),
            ],
        ),
        Prompt(
            name="market_overview",
            description="Get a market trend overview for a commodity.",
            arguments=[
                PromptArgument(name="commodity", description="Commodity name", required=True),
                PromptArgument(name="location", description="State or city", required=True),
            ],
        ),
        Prompt(
            name="transport_compare",
            description="Compare transportation costs from a farm location.",
            arguments=[
                PromptArgument(name="farm_location", description="Farm location", required=True),
            ],
        ),
        Prompt(
            name="crop_fundamentals",
            description="Get acreage, yield, and production for a commodity and location.",
            arguments=[
                PromptArgument(name="commodity", description="Commodity name", required=True),
                PromptArgument(name="location", description="State or city", required=True),
                PromptArgument(name="year", description="Crop year", required=False),
            ],
        ),
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: dict):
    args = arguments or {}
    sanitized = {key: sanitize_input(value) for key, value in args.items()}

    if name == "selling_decision":
        commodity = sanitized.get("commodity", "corn")
        farm_location = sanitized.get("farm_location", "Ames, IA")
        volume = sanitized.get("volume_bushels")
        volume_text = f" {volume} bushels" if volume else ""
        text = (
            f"Where should I sell{volume_text} of {commodity} from {farm_location}? "
            "Please rank the best selling locations by net price."
        )
    elif name == "market_overview":
        commodity = sanitized.get("commodity", "corn")
        location = sanitized.get("location", "Iowa")
        text = f"Show me the weekly market trends for {commodity} in {location}."
    elif name == "transport_compare":
        farm_location = sanitized.get("farm_location", "Ames, IA")
        text = f"Compare transportation costs from {farm_location} for common shipping modes."
    elif name == "crop_fundamentals":
        commodity = sanitized.get("commodity", "corn")
        location = sanitized.get("location", "Iowa")
        year = sanitized.get("year")
        year_text = f" for {year}" if year else ""
        text = (
            f"Show me planted acreage, yield, and production for {commodity} in "
            f"{location}{year_text}."
        )
    else:
        raise ValueError(f"Unknown prompt: {name}")

    return GetPromptResult(
        description=f"Prompt: {name}",
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
    )


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
