# AgriConnect MCP - Current Architecture

This README documents the current codebase as it exists in `src/`, `dashboard/`, and the project root.

## Overview

AgriConnect is a Python Model Context Protocol server that turns USDA data into structured decision-support tools.

The current implementation centers on three active workflow areas:
- `crop fundamentals`: planted acreage, yield, and production from USDA NASS QuickStats
- `transportation analysis`: barge, truck, spread-based, and index-context transport information from USDA AgTransport
- `selling analysis`: ranked selling options and profit simulation using USDA AMS prices plus transport costs

At a high level, the codebase is split into:
- `server surface`: MCP tool schemas, resource registration, prompt registration, and tool dispatch
- `tool layer`: user-facing workflows that format results as readable text
- `client layer`: source-specific USDA API fetchers and normalizers
- `shared infrastructure`: security, caching, observability, and geographic resolution

## Active Tool Surface

The currently implemented non-trend tools are:

- `get_cash_prices`
  Returns a formatted cash-price table for a commodity near a location using USDA AMS.
- `get_transportation_costs`
  Returns formatted transport cost rows for a farm location and commodity using USDA AgTransport datasets.
- `rank_selling_options`
  Combines AMS cash prices with transport rates to rank selling options by estimated net price.
- `simulate_profit`
  Multiplies ranked net prices by bushel volume to estimate total revenue.
- `get_crop_fundamentals`
  Returns planted acreage, yield, and production for a commodity, location, and optional year using USDA NASS.

## System Architecture

```text
MCP Client
  -> stdio / JSON-RPC
  -> src/server.py
     -> security.py
     -> observability.py
     -> tools/*
        -> clients/*
        -> utils/geo.py
        -> cache.py

clients/ams_client.py
  -> USDA AMS MyMarketNews

clients/transport_client.py
  -> USDA AgTransport Socrata datasets

clients/nass_client.py
  -> USDA NASS QuickStats
```

The architecture is intentionally modular:
- `src/server.py` knows how to validate, trace, and dispatch
- each `src/tools/*.py` file owns one user-facing workflow
- each `src/clients/*.py` file owns one USDA source family
- shared logic lives in `src/security.py`, `src/cache.py`, `src/observability.py`, and `src/utils/geo.py`

That makes it straightforward to add future commodity families such as cattle, hogs, cotton, fruits, or specialty crops without redesigning the MCP layer.

### Detailed Architecture Diagram

```text
┌──────────────────────────────────────────────────────────────────────┐
│                            MCP CLIENT                                │
│                  (Claude Desktop, Cursor, etc.)                      │
│                                                                      │
│   Natural language query -> tool selection -> structured tool call   │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               │ MCP Protocol (JSON-RPC over stdio)
                               │ tool calls, resources, prompts
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                            src/server.py                             │
│                         MCP Server Surface                           │
│                                                                      │
│  • Registers static tool schemas                                     │
│  • Registers MCP resources                                           │
│  • Registers MCP prompts                                             │
│  • Dispatches tool calls                                             │
│  • Wraps calls with security + tracing                               │
└───────────────┬──────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                            src/security.py                           │
│                           Security Layer                             │
│                                                                      │
│  1. rate_limiter.check()                                             │
│  2. sanitize_input()                                                 │
│  3. validate_tool_args()                                             │
│  4. sanitize_output() after tool execution                           │
│                                                                      │
│  Protections: schema validation, input filtering, output filtering,  │
│  secret redaction, global request limiting                           │
└───────────────┬──────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         src/observability.py                         │
│                        Observability Layer                           │
│                                                                      │
│  • trace_span() opens and closes tool spans                          │
│  • child_span() measures nested calls                                │
│  • log_audit_event() writes audit records                            │
│                                                                      │
│  Outputs: server.log, server.log.jsonl, traces.jsonl, audit.jsonl    │
└───────────────┬──────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                           Tool Layer                                 │
│                          src/tools/*                                 │
│                                                                      │
│  prices.py        -> get_cash_prices()                               │
│  transport.py     -> get_transportation_costs()                      │
│  analysis.py      -> rank_selling_options()                          │
│                      simulate_profit()                               │
│  fundamentals.py  -> get_crop_fundamentals()                         │
│                                                                      │
│  Tools format human-readable outputs and orchestrate source calls    │
└───────────────┬──────────────────────────────────────────────────────┘
                │
                ├──────────────────────────────┬────────────────────────┐
                │                              │                        │
                ▼                              ▼                        ▼
┌────────────────────────────┐  ┌────────────────────────────┐  ┌────────────────────────────┐
│ src/clients/ams_client.py  │  │ src/clients/transport_    │  │ src/clients/nass_client.py │
│                            │  │ client.py                  │  │                            │
│ USDA AMS MyMarketNews      │  │ USDA AgTransport          │  │ USDA NASS QuickStats       │
│                            │  │ (Socrata)                 │  │                            │
│ • Static slug registry     │  │ • Barge spot rates        │  │ • Acreage/yield/production │
│ • Detail-first fetch       │  │ • Truck rates             │  │ • Rate-limited fetches     │
│ • Header narrative fallback│  │ • Price spreads           │  │ • Cached QuickStats rows   │
│ • Regex commodity parsing  │  │ • Cost indicators         │  │                            │
│ • AMS auth + retries       │  │ • Commodity-aware costs   │  │                            │
└───────────────┬────────────┘  └───────────────┬────────────┘  └───────────────┬────────────┘
                │                               │                               │
                ▼                               ▼                               ▼
┌────────────────────────────┐  ┌────────────────────────────┐  ┌────────────────────────────┐
│ USDA AMS API               │  │ USDA AgTransport          │  │ USDA NASS API              │
│ marsapi.ams.usda.gov       │  │ agtransport.usda.gov      │  │ quickstats.nass.usda.gov   │
│                            │  │                           │  │                            │
│ Grain bid reports by slug  │  │ deqi-uken  barge          │  │ State-level crop           │
│ Detail + Header sections   │  │ fxkn-2w9c truck           │  │ fundamentals               │
│                            │  │ an4w-mnp7 spreads         │  │                            │
│                            │  │ 8uye-ieij indices         │  │                            │
└────────────────────────────┘  └────────────────────────────┘  └────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│                         Shared Infrastructure                         │
│                                                                      │
│  src/cache.py                                                        │
│    • Shared TTL cache                                                │
│    • 500-entry capacity                                              │
│    • Background expiration sweep                                     │
│                                                                      │
│  src/utils/geo.py                                                    │
│    • Location -> (state, lat, lon) resolution                        │
│    • City/state lookup                                               │
│    • State centroids                                                 │
│    • Haversine distance estimates                                    │
└──────────────────────────────────────────────────────────────────────┘
```

## Request Lifecycle

The current selling workflow works like this:

### Detailed Request Lifecycle Diagram

```text
┌──────────────────────────────────────────────────────────────────────┐
│ 1. MCP client sends tool call                                        │
│    Example: rank_selling_options(commodity="corn",                   │
│             farm_location="Ames, IA")                                │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 2. src/server.py receives the request                                │
│    • looks up the tool schema                                        │
│    • prepares sanitized argument storage                             │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 3. src/security.py pre-processing                                    │
│    • rate_limiter.check()                                            │
│    • sanitize_input() on every string argument                       │
│    • validate_tool_args() against JSON schema                        │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 4. src/observability.py opens a trace span                           │
│    • request_id generated                                            │
│    • tool name and arg keys recorded                                 │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 5. src/tools/analysis.py begins selling workflow                     │
│    • resolve_location(farm_location)                                 │
│    • returns (state, lat, lon)                                       │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                 ┌─────────────┴─────────────┐
                 │                           │
                 ▼                           ▼
┌──────────────────────────────┐  ┌───────────────────────────────────┐
│ 6a. AMS price fetches        │  │ 6b. AgTransport fetches          │
│     src/clients/ams_client.py│  │     src/clients/transport_client │
│                              │  │                                   │
│ • origin state + nearby      │  │ • barge spot rates               │
│   states                     │  │ • truck rates                    │
│ • detail section first       │  │ • price spreads                  │
│ • header narrative fallback  │  │ • cost indicators                │
│ • fallback sample prices if  │  │ • fallback sample transport if   │
│   no live data               │  │   no live data                   │
└───────────────┬──────────────┘  └───────────────┬───────────────────┘
                │                                 │
                └─────────────┬───────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 7. src/tools/analysis.py builds selling options                      │
│    • filters transport rows with usable rate_per_bushel              │
│    • estimates market distance with haversine()                      │
│    • applies heuristic for rates < 0.05 as per-mile                  │
│    • computes net_price = cash_price - transport_cost                │
│    • deduplicates by (market, mode)                                  │
│    • sorts descending by net_price                                   │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 8. Tool formats human-readable output                                │
│    • ranked selling table or profit table                            │
│    • optional notes for fallback/sample conditions                   │
│    • optional notes for requested radius                             │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 9. src/security.py post-processing                                   │
│    • sanitize_output() strips unsafe role/tool-like content          │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 10. src/observability.py closes trace                                │
│     • writes traces.jsonl entry                                      │
│     • writes audit.jsonl success or error entry                      │
│     • server logs updated                                            │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 11. MCP client receives final text response                          │
│     • safe, formatted, human-readable output                         │
└──────────────────────────────────────────────────────────────────────┘
```

1. An MCP client sends a tool call such as `rank_selling_options`.
2. `src/server.py` validates the tool arguments against a static JSON schema.
3. `src/security.py` sanitizes string inputs and enforces the global rate limiter.
4. `src/observability.py` opens a trace span for the tool call.
5. `src/utils/geo.py` resolves the farm location into a tuple:
   `(state_code, latitude, longitude)`.
6. `src/tools/analysis.py` fetches AMS prices for the origin state and configured nearby states.
7. `src/tools/analysis.py` fetches transport rows for the origin state from AgTransport.
8. `src/tools/analysis.py` builds selling options by combining each price row with each usable transport row.
9. Market distance is estimated with `haversine()` when coordinates are available.
10. If a transport rate is below `0.05`, the analysis currently treats it as a per-mile heuristic and multiplies it by estimated distance.
11. The tool sorts options by `net_price = cash_price - transport_cost`.
12. `src/server.py` sanitizes output text, closes the trace span, logs the audit event, and returns the formatted response.

## Component Breakdown

### `src/server.py`

`src/server.py` is the MCP entry point. It is responsible for:
- registering static tool schemas
- registering MCP resources
- registering MCP prompts
- dispatching tool calls
- wrapping dispatch in security and observability

Current non-trend tool schemas described in code:
- `get_cash_prices`
- `get_transportation_costs`
- `rank_selling_options`
- `simulate_profit`
- `get_crop_fundamentals`

Current resources:
- `usda://commodities/supported`
- `usda://markets/regions`
- `usda://status`

Current `SUPPORTED_COMMODITIES` in code:
- `corn`
- `soybeans`
- `wheat`
- `oats`
- `sorghum`

Current `MARKET_REGIONS` in code:
- `Midwest`
- `Plains`
- `Gulf`
- `River`

Current prompts in code:
- `selling_decision`
- `market_overview`
- `transport_compare`
- `crop_fundamentals`

Important implementation detail:
- tools are statically registered in source and not created dynamically from external input

### `src/security.py`

`src/security.py` provides four main protections:

- `sanitize_input()`
  Trims input, enforces a max length of 200 characters, and blocks strings that match compiled injection patterns.
- `validate_tool_args()`
  Enforces required fields, expected types, min/max constraints, enum allowlists, and `additionalProperties: false`.
- `RateLimiter`
  Uses a rolling-window timestamp deque to limit requests to 30 per 60 seconds.
- `sanitize_output()`
  Removes null characters, role-injection prefixes, and tool-tag-looking output before text is returned to the MCP client.

It also contains `redact_secrets()` for log safety.

### `src/cache.py`

`src/cache.py` implements an in-memory TTL cache:
- dictionary-backed
- thread-safe
- 500-entry default capacity
- evicts expired entries in a background daemon thread every 5 minutes
- uses SHA-256 cache keys based on URL plus sorted params

`DEFAULT_CACHE` is shared across the server.

### `src/observability.py`

`src/observability.py` implements:
- text logging to `server.log`
- JSON logging to `server.log.jsonl`
- trace events in `traces.jsonl`
- audit events in `audit.jsonl`

The core tracing object is `ToolCallSpan`, which stores:
- tool name
- start time
- request id
- duration
- outcome
- child spans

Tools can attach nested timings with `span.child_span(...)`.

### `src/clients/ams_client.py`

`src/clients/ams_client.py` handles AMS MyMarketNews integration.

Key characteristics:
- uses a static slug registry by commodity and state
- tries `Report Detail` first, then falls back to `Report Header`
- parses structured rows when available
- falls back to regex extraction from `report_narrative`
- appends barge and export slugs to state lookups
- requires `USDA_AMS_API_KEY`
- retries up to 3 times with exponential backoff
- enforces a domain allowlist

Important current details reflected in code:
- regex patterns are defined for `corn`, `soybeans`, and `wheat`
- `_extract_price_from_narrative()` lowercases and normalizes whitespace before regex search
- detail cache keys include `section`, `commodity`, and `state`
- header cache uses only the base report URL
- fallback sample prices are available through `fallback_prices()`

### `src/clients/transport_client.py`

`src/clients/transport_client.py` integrates with USDA AgTransport via Socrata.

It currently pulls from four dataset families:
- `deqi-uken`: barge spot rates
- `fxkn-2w9c`: truck rates
- `an4w-mnp7`: price spreads
- `8uye-ieij`: transport cost indicators

Important current logic:
- barge percentage-of-tariff values are converted into dollars per bushel
- truck column names are discovered from live rows and extracted using candidate-field probing
- spread rows are treated as implied transport cost candidates
- cost-indicator rows are returned as context with `rate_per_bushel=None`
- each dataset can fail independently without collapsing the whole transport response

Returned transport rows follow a normalized shape:
- `mode`
- `origin_region`
- `destination`
- `rate_per_bushel`
- `note`
- `source_dataset`

### `src/clients/nass_client.py`

`src/clients/nass_client.py` currently implements crop fundamentals only.

What it does:
- rate-limits NASS calls using a per-process 24-hour timestamp log
- fetches rows from the QuickStats API with retry and cache support
- normalizes acreage/yield/production rows
- queries metrics through `_fetch_fundamental_metric()`
- assembles a snapshot with `fetch_crop_fundamentals()`

Current implementation notes:
- the query config in `FUNDAMENTAL_QUERIES` is defined only for `corn`
- `fetch_crop_fundamentals()` loops over `planted_acres`, `yield`, and `production`
- `_fetch_quickstats_rows()` returns `[]` on failure rather than raising

### `src/tools/prices.py`

`src/tools/prices.py` formats cash-price results for users.

Current behavior:
- resolves a location to `(state, lat, lon)` and uses only the state code
- fetches AMS prices for the state
- falls back to AMS sample prices on failure
- sorts by descending `cash_price`
- formats a simple table with location, state, type, cash, basis, and report date

### `src/tools/transport.py`

`src/tools/transport.py` formats transportation rows for users.

Current behavior:
- resolves a location to `(state, lat, lon)` and uses only the state code
- fetches transport rows for the origin state and commodity
- supports optional mode filtering
- shows `N/A (index)` for rows with `rate_per_bushel=None`
- falls back to sample transport rows if live fetches fail

Mode note:
- the tool schema only accepts `truck`, `rail`, or `barge` as filters
- the transport client can also return `rail_or_barge` rows

### `src/tools/fundamentals.py`

`src/tools/fundamentals.py` provides the acreage/yield/production snapshot tool.

Current behavior:
- resolves a location to `(state, lat, lon)` and uses only the state code
- defaults `year` to `datetime.now().year - 1`
- calls `nass_client.fetch_crop_fundamentals()`
- formats a table with metric, value, unit, year, and source
- adds a short one-line snapshot summary above the table

Important current note:
- the code attempts to use `nass_client.fallback_crop_fundamentals(...)` if the fetch raises or returns nothing, but that fallback helper is not currently defined in `src/clients/nass_client.py`

### `src/tools/analysis.py`

`src/tools/analysis.py` is the core selling and profit workflow.

Current logic includes:
- `NEARBY_STATES` for a limited set of origin states
- farm-location resolution to state plus coordinates
- AMS price fetches across the origin state and configured nearby states
- transport fetches for the origin state only
- `haversine()`-based distance calculation
- a heuristic that treats rates under `0.05` as per-mile and multiplies them by estimated distance
- sorting by estimated `net_price`

`rank_selling_options()` returns the top 10 ranked options.

`simulate_profit()` returns top options with:
- net price per bushel
- volume
- total revenue

### `src/utils/geo.py`

`src/utils/geo.py` currently does more than state resolution.

It provides:
- `_resolve_state_only()`
- `resolve_location() -> (state, lat, lon)`
- `haversine()`
- `CITY_COORDS`
- `STATE_CENTROIDS`

Current supported resolution patterns:
- `"City, ST"`
- `"City, State"`
- direct state names from `STATE_ALIASES`
- direct city names from `CITY_STATE_LOOKUP`
- 2-letter abbreviations

Important code-level details:
- only a small set of cities and states currently have explicit coordinate coverage
- unresolved locations default to Iowa
- results are cached for 24 hours

### `dashboard/app.py`

The Streamlit dashboard reads logs from `LOG_DIR` and displays:
- total calls
- success rate
- errors
- average latency
- security event count

It also shows:
- trace tables
- audit events
- raw server logs

### Empty / Placeholder Files

These files currently exist but are empty:
- `mcp.json`
- `docker-compose.yml`
- `Dockerfile`
- `tests/test_all.py`
- `src/clients/__init__.py`
- `src/tools/__init__.py`
- `src/utils/__init__.py`

## Data Contracts

### AMS Price Rows

`src/clients/ams_client.py` returns price rows in this normalized shape:

```python
{
    "location_name": str,
    "state": str,
    "market_type": str,
    "cash_price": float,
    "basis": float,
    "report_date": str,
    "data_source": str,
}
```

### Transport Rows

`src/clients/transport_client.py` returns transport rows in this normalized shape:

```python
{
    "mode": str,
    "origin_region": str,
    "destination": str,
    "rate_per_bushel": float | None,
    "note": str,
    "source_dataset": str,
}
```

### Crop Fundamental Rows

`src/clients/nass_client.py` normalizes acreage/yield/production rows like this:

```python
{
    "year": str,
    "Value": str,
    "commodity_desc": str,
    "state_alpha": str,
    "unit_desc": str,
    "short_desc": str,
    "source_desc": str,
}
```

## Current Resources

The server currently exposes these MCP resources:
- `usda://commodities/supported`
- `usda://markets/regions`
- `usda://status`

`usda://status` returns:
- `version`
- `cache` stats
- `rate_limiter` status

## Current Prompts

The server currently registers these prompts:
- `selling_decision`
- `market_overview`
- `transport_compare`
- `crop_fundamentals`

For this README, only `selling_decision`, `transport_compare`, and `crop_fundamentals` are in active project scope.

## Configuration

All configuration is environment-driven.

| Variable | Default | Purpose |
|----------|---------|---------|
| `USDA_AMS_API_KEY` | `""` | AMS API key used for MyMarketNews requests |
| `USDA_NASS_API_KEY` | `"DEMO_KEY"` | NASS QuickStats API key |
| `SOCRATA_APP_TOKEN` | `""` | Optional AgTransport app token |
| `LOG_LEVEL` | `"INFO"` | Python logger level |
| `LOG_DIR` | `"/tmp/agriconnect-logs"` | Output directory for logs |
| `NASS_DAILY_LIMIT` | `50` | Max NASS requests per day |

## Dependencies

`requirements.txt` currently contains:
- `mcp`
- `httpx`
- `python-dotenv`
- `streamlit`

## File Structure

```text
agriconnect/
├── README.md
├── requirements.txt
├── mcp.json
├── docker-compose.yml
├── Dockerfile
├── dashboard/
│   └── app.py
├── src/
│   ├── server.py
│   ├── security.py
│   ├── observability.py
│   ├── cache.py
│   ├── clients/
│   │   ├── __init__.py
│   │   ├── ams_client.py
│   │   ├── transport_client.py
│   │   └── nass_client.py
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── prices.py
│   │   ├── transport.py
│   │   ├── analysis.py
│   │   ├── fundamentals.py
│   │   └── trends.py
│   └── utils/
│       ├── __init__.py
│       └── geo.py
└── tests/
    └── test_all.py
```

## Current Limitations and Notes

These points reflect the code as it exists today:

- `src/tools/trends.py` is present but intentionally out of scope for this README.
- `src/server.py` still registers trend tools and a market-overview prompt.
- `src/clients/nass_client.py` currently implements crop fundamentals only; it does not currently expose a price-history helper.
- `src/server.py` allows `corn` and `soybeans` for `get_crop_fundamentals`, but `src/clients/nass_client.py` currently defines query config only for `corn`.
- `src/tools/fundamentals.py` references fallback helpers that are not currently implemented in `src/clients/nass_client.py`.
- `src/tools/analysis.py` uses a limited nearby-state map rather than nationwide adjacency logic.
- `src/utils/geo.py` uses a small coordinate lookup table and falls back to state centroids for many locations.
- `src/tools/transport.py` can display `rail_or_barge` rows, but the tool schema does not currently allow filtering by `rail_or_barge`.
- `tests/test_all.py` is currently an empty placeholder.

## Why This Architecture Still Scales

Even with the current project scope focused on grain workflows, the codebase is modular enough to expand cleanly:
- add a new client file for a new USDA source family
- add a new tool file that formats a new workflow
- register the schema and dispatch path in `src/server.py`
- reuse the existing security, cache, observability, and location infrastructure

That design is the main reason AgriConnect can grow beyond corn and soybean workflows into livestock, specialty crops, fiber, or broader commodity-market use cases without rebuilding the MCP layer.
