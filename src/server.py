import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("agriconnect-mcp")

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="get_cash_prices",
            description="Get current cash commodity prices near a farm location.",
            inputSchema={
                "type": "object",
                "properties": {
                    "commodity": {
                        "type": "string",
                        "description": "The commodity, e.g. corn, soybeans, wheat"
                    },
                    "location": {
                        "type": "string",
                        "description": "Farm location, e.g. Ames, IA"
                    }
                },
                "required": ["commodity", "location"]
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "get_cash_prices":
        commodity = arguments.get("commodity", "unknown")
        location = arguments.get("location", "unknown")
        return [
            TextContent(
                type="text",
                text=f"[STUB] Cash prices for {commodity} near {location} — real data coming soon."
            )
        ]
    raise ValueError(f"Unknown tool: {name}")

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )

if __name__ == "__main__":
    asyncio.run(main())