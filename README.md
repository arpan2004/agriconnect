# AgriConnect MCP — Architecture & System Design

---

## Table of Contents

1. [Overview](#overview)
2. [System Architecture](#system-architecture)
3. [Request Lifecycle](#request-lifecycle)
4. [Component Breakdown](#component-breakdown)
5. [Data Layer](#data-layer)
6. [Security Design](#security-design)
7. [Observability Design](#observability-design)
8. [Resilience Design](#resilience-design)
9. [MCP Integration](#mcp-integration)
10. [File Structure](#file-structure)
11. [Configuration Reference](#configuration-reference)
12. [Key Design Decisions](#key-design-decisions)

---

## Overview

AgriConnect MCP is a Model Context Protocol server that exposes USDA commodity data through structured tools that any MCP-compatible AI client can call. It bridges three USDA data sources — AMS MyMarketNews (cash grain prices), AgTransport via Socrata (transportation rates), and NASS QuickStats (historical statistics) — and adds an analysis layer that combines price and transportation cost data to produce ranked selling recommendations.

The server is designed around five properties: correctness of data, security of inputs and outputs, observability of every operation, resilience to external API failure, and full compliance with the MCP specification.

**Primary use case:** A farmer asks a natural language question like *"Where should I sell 250,000 bushels of corn from Des Moines, Iowa?"* The MCP client routes that question to this server, which fetches live USDA grain prices from AMS and live transport rates from the USDA AgTransport platform, cross-joins them into net-price-per-bushel options, and returns a ranked list of selling locations.

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          MCP CLIENT                                  │
│                  (Claude Desktop, Cursor, etc.)                      │
│                                                                      │
│   Natural language query → tool selection → structured tool call     │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              │  MCP Protocol (JSON-RPC over stdio)
                              │  tool calls, resource reads, prompts
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          server.py                                   │
│                       MCP Server Core                                │
│                                                                      │
│  • Registers tools, resources, and prompt templates with MCP SDK     │
│  • Receives tool call requests from client                           │
│  • Routes each call through: security → trace → dispatch → return   │
│  • Exposes health/status as a readable MCP resource                  │
└──────────┬───────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       Security Pipeline                              │
│                         security.py                                  │
│                                                                      │
│  Step 1: rate_limiter.check()                                        │
│          Token bucket — 30 req/min. Reject early if over limit.      │
│                                                                      │
│  Step 2: sanitize_input(args)                                        │
│          Strip whitespace. Enforce max length. Regex scan for        │
│          shell metacharacters, SQL patterns, template injection,     │
│          prompt injection phrases, role injection patterns.          │
│                                                                      │
│  Step 3: validate_tool_args(args, schema)                            │
│          Enforce required fields, enum allowlists, numeric ranges,   │
│          and additionalProperties: false on every tool schema.       │
│                                                                      │
│  On any failure → log audit event → return error string to client   │
└──────────┬───────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    Observability Wrapper                             │
│                      observability.py                                │
│                                                                      │
│  trace_tool_call() opens a span before dispatch, closes after.      │
│  Captures: tool name, arg keys (not values), latency, outcome.      │
│                                                                      │
│  Writes to three logs:                                               │
│    traces.jsonl  — one entry per tool call with full timing          │
│    audit.jsonl   — security events, errors, rate limit hits          │
│    server.log    — human-readable structured application log         │
└──────────┬───────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         Tool Layer                                   │
│                    src/tools/*.py                                    │
│                                                                      │
│  prices.py      get_cash_prices()          → price table            │
│  transport.py   get_transportation_costs() → rate table             │
│  analysis.py    rank_selling_options()     → ranked net profit list │
│                 simulate_profit()          → total revenue table    │
│  trends.py      get_market_trends()        → price history table    │
│                 get_weekly_summary()       → narrative paragraph    │
│                                                                      │
│  Tools are pure functions: receive validated args, call data layer,  │
│  format output as plain text, return string. No side effects.        │
└──────────┬───────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         Cache Layer                                  │
│                          cache.py                                    │
│                                                                      │
│  Every API call checks cache first. Key = SHA-256(url + params).    │
│                                                                      │
│  TTLs by data type:                                                  │
│    Cash prices (AMS)       15 minutes   (USDA updates ~twice/day)   │
│    Transport rates         6 hours      (Socrata updates weekly)     │
│    NASS history            1 hour       (weekly publication cadence) │
│    Geo lookups             24 hours     (static reference data)      │
│    Health checks           30 seconds                               │
│                                                                      │
│  Max 500 entries. LRU eviction at capacity. Background sweep        │
│  every 5 minutes to expire stale entries.                            │
└──────────┬───────────────────────────────────────────────────────────┘
           │  cache miss only
           │
           ├────────────────────┬────────────────────────┐
           ▼                    ▼                        ▼
┌──────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
│  ams_client.py   │  │ transport_client.py  │  │   nass_client.py     │
│                  │  │                      │  │                      │
│ fetch_grain_     │  │ fetch_transport_     │  │ fetch_commodity_     │
│ prices()         │  │ rates()              │  │ prices()             │
│                  │  │                      │  │                      │
│ Static registry  │  │ Fetches from 4       │  │ Daily rate limit     │
│ maps commodity + │  │ Socrata datasets:    │  │ tracker (50 req/day  │
│ state → slug IDs │  │  • Barge spot rates  │  │ on free NASS tier)   │
│                  │  │  • Truck rates       │  │                      │
│ Tries Report     │  │  • Price spreads     │  │ Retry: 2×            │
│ Detail section   │  │  • Cost indicators   │  │ Timeout: 15s         │
│ first, falls     │  │                      │  │                      │
│ back to Header   │  │ No API key required  │  │ URL allowlist        │
│ narrative        │  │ (public platform).   │  │                      │
│                  │  │ Optional app token   │  │                      │
│ Retry: 3×        │  │ raises rate limit.   │  │                      │
│ Backoff: 1.5^n s │  │                      │  │                      │
│ Timeout: 12s     │  │ Timeout: 10s         │  │                      │
│ URL allowlist    │  │ URL allowlist        │  │                      │
└────────┬─────────┘  └──────────┬───────────┘  └──────────┬───────────┘
         │                       │                          │
         ▼                       ▼                          ▼
┌──────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
│  USDA AMS API    │  │  USDA AgTransport    │  │  USDA NASS           │
│                  │  │  (Socrata platform)  │  │  QuickStats API      │
│ marsapi.ams      │  │                      │  │                      │
│ .usda.gov        │  │ agtransport.usda.gov │  │ quickstats.nass      │
│                  │  │                      │  │ .usda.gov/api        │
│ Cash grain bids  │  │ deqi-uken  Barge     │  │                      │
│ by state and     │  │ fxkn-2w9c  Truck     │  │ Weekly prices        │
│ elevator, pulled │  │ an4w-mnp7  Spreads   │  │ received by farmers  │
│ via slug IDs     │  │ 8uye-ieij  Indices   │  │ Production stats     │
└──────────────────┘  └──────────────────────┘  └──────────────────────┘
```

---

## Request Lifecycle

This is the exact sequence of operations for every tool call, from client query to response.

```
1.  MCP client sends tool call request
      { name: "rank_selling_options",
        arguments: { commodity: "corn", farm_location: "Des Moines, IA" } }

2.  server.py receives via stdio transport

3.  Rate limiter checks rolling 60-second window
      → Over limit: log audit event, return error string immediately
      → Under limit: continue

4.  sanitize_input() runs on every string argument
      → Injection pattern detected: log audit event, raise ValueError
      → Clean: continue with sanitized strings

5.  validate_tool_args() checks against JSON Schema
      → Invalid enum, missing required field, out-of-range number: raise ValueError
      → Valid: continue

6.  trace_tool_call() opens a span, records start time and request ID

7.  Tool function dispatched:
      rank_selling_options(commodity="corn", farm_location="Des Moines, IA")

8.  geo.resolve_location("Des Moines, IA") → "IA"
      → Cache check: HIT → return "IA" immediately
      → Cache miss: resolve from lookup table, write to cache (TTL 24h)

9.  Prices and transport fetched INDEPENDENTLY (not gathered — separate fallbacks)

    9a. fetch_grain_prices("corn", "IA") — AMS MyMarketNews
          → Registry lookup: commodity="corn", state="IA" → slug "2850"
          → Cache HIT: return immediately
          → Cache MISS:
              i.  GET /reports/2850?section=Report+Detail (structured rows)
              ii. If Detail empty → GET /reports/2850 (Report Header)
                  Parse commodity price from report_narrative text:
                  "State Average Price: Corn -- $4.08 ..."
              iii. On failure → return labeled fallback prices

    9b. fetch_transport_rates("IA", "corn") — AgTransport / Socrata
          → Cache HIT: return immediately
          → Cache MISS, queries four Socrata datasets:
              i.  deqi-uken barge spot rates for "Mid-Mississippi" segment
                  Convert % of tariff → $/bu using 1976 benchmark ($5.32/ton)
                  and short-ton bushel weight (corn: 35.714 bu/ton)
              ii. fxkn-2w9c quarterly truck rates for "North Central" region
                  Column names discovered at runtime via unfiltered fetch
              iii. an4w-mnp7 price spreads for Iowa corn → Gulf/PNW
                   |spread| used as implied transport cost
              iv. 8uye-ieij weekly cost indices for context
          → Each dataset fails independently; partial results returned

10. _build_selling_options() cross-joins prices × transport rates
      → Filters to rows where rate_per_bushel is not None
        (cost-index-only rows skipped — no usable rate to subtract)
      → net_price = cash_price - rate_per_bushel
      → Deduplicate by (market, mode) pair
      → Sort descending by net_price

11. Format result as plain text string
      → volume formatted with commas (250,000 not 250000)
      → N/A shown for index-only transport rows

12. sanitize_output() scans response for role injection patterns, strips nulls

13. trace_tool_call() closes span, writes to traces.jsonl

14. log_audit_event("TOOL_SUCCESS", ...) writes to audit.jsonl

15. Return TextContent to MCP client
```

---

## Component Breakdown

### server.py

The MCP entry point. Has three responsibilities and nothing else.

First, it declares the tool manifest — the list of Tool objects with names, descriptions, and JSON Schemas. This is static and version-controlled. No tool is registered dynamically from any external input. This is a direct mitigation of the tool poisoning attack vector.

Second, it implements the MCP handlers: `list_tools`, `call_tool`, `list_resources`, `read_resource`, `list_prompts`, `get_prompt`. These are thin — they do security, logging, and dispatch, but contain no business logic themselves.

Third, it exposes three MCP resources that any client can read without making a tool call: the list of supported commodities, the list of USDA market regions, and a live server status endpoint that returns cache stats and rate limiter state.

### security.py

Runs before every tool call. Implements six explicit defenses.

`sanitize_input()` applies a compiled regex against ten injection patterns before any string argument touches the application. The patterns cover shell metacharacters, SQL comment syntax, HTML/XML tags, double-brace and dollar-brace template injection, and several known prompt injection phrases. The regex is compiled once at module load with `re.IGNORECASE`. Strings over 200 characters are truncated before regex evaluation.

`validate_tool_args()` enforces the JSON Schema for each tool. It checks required fields, validates enum values against explicit allowlists, enforces numeric minimum and maximum bounds, and rejects any field not declared in the schema (`additionalProperties: false`). This prevents parameter pollution and ensures the application only processes inputs it explicitly expected.

`RateLimiter` is a thread-safe token bucket. It maintains a list of request timestamps and counts only those within a 60-second rolling window. At 30 requests the next call returns False without acquiring a lock — the check is cheap.

`redact_secrets()` scans any text for patterns matching API key, token, secret, password, and bearer credential formats, and replaces matched values with `[REDACTED]`. This runs on log output to ensure keys set in environment variables never appear in log files even if accidentally included in an error message.

`sanitize_output()` runs on the formatted string before it returns to the MCP client. It strips role injection patterns (`system:`, `user:`, `assistant:` at line start) and any MCP tool call tags that a malicious or compromised API response might include to hijack the client's next action.

### cache.py

A TTL cache backed by a Python dictionary. Every entry stores a tuple of `(value, expires_at)` where `expires_at` is a Unix timestamp. On `get()`, if the current time exceeds `expires_at`, the entry is deleted and None is returned. On `set()`, if the store is at its 500-entry capacity, the entry with the earliest expiry is evicted first.

A daemon thread runs every 5 minutes and calls `_evict_expired()` to prevent unbounded memory accumulation in long-running deployments. Cache keys are SHA-256 hashes of the URL plus sorted parameter dict, truncated to 32 hex characters.

### ams_client.py

Handles all communication with USDA AMS MyMarketNews. Uses a **static registry** (`GRAIN_REPORT_REGISTRY`) that maps `(commodity, state)` pairs directly to confirmed slug IDs, populated from a one-time exploration of the `/reports` index (1,049 reports as of April 2026). No runtime index scan occurs.

**Report section strategy:** Each slug fetch tries two sections in order:

1. `GET /reports/<slug>?section=Report+Detail` — structured per-elevator rows with explicit price fields. Filtered by commodity name against each row.
2. `GET /reports/<slug>` (Report Header) — falls back to parsing the `report_narrative` text field using commodity-specific regex patterns. Example narrative: `"State Average Price: Corn -- $4.08 (-.39K) Down 2 cents"`. Rows where `report_narrative` is null are silently skipped.

Barge and export supplement slugs are automatically appended to every state lookup to give the selling-options tool more destination market prices.

The API key is read from `USDA_AMS_API_KEY` and sent as HTTP Basic auth. It is never accepted as a function argument and never appears in logs.

Retry logic: up to 3 attempts with backoff of `1.5^attempt` seconds. HTTP 429 reads the `Retry-After` header. HTTP 4xx fails fast. All retries exhausted raises `ConnectionError`, which triggers the fallback path in the tool layer.

### transport_client.py

Handles all communication with the **USDA AgTransport platform** (`agtransport.usda.gov`), a public Socrata instance. No API key is required. The optional `SOCRATA_APP_TOKEN` environment variable raises the anonymous rate limit from 1 to 1,000 requests per second.

Queries four datasets using SoQL (Socrata Query Language — SQL-like syntax documented at `dev.socrata.com/docs/queries/`):

| Dataset ID | Name | What it provides |
|------------|------|-----------------|
| `deqi-uken` | Downbound Grain Barge Rates | Weekly spot rate as % of 1976 tariff benchmark, by river segment |
| `fxkn-2w9c` | Quarterly Grain Truck Rates | $/bu by region and haul distance, quarterly cadence |
| `an4w-mnp7` | Grain Price Spreads | Origin-to-export $/bu differential — best proxy for total transport cost |
| `8uye-ieij` | Grain Transport Cost Indicators | Weekly mode indices (truck, rail, barge) — context only, no direct $/bu |

**Barge rate conversion:** The `rate` column in `deqi-uken` is a percentage of the 1976 Waterways Freight Bureau Tariff No. 7 benchmark. Conversion to $/bushel:

```
$/ton  = (rate_pct / 100) × benchmark_$/short_ton
$/bu   = $/ton / (2000 lbs/ton ÷ lbs_per_bushel)

Benchmarks (official, from USDA AMS GTR and dataset description):
  Twin Cities     $6.19/ton    Mid-Mississippi  $5.32/ton
  Illinois        $4.64/ton    St. Louis        $3.99/ton
  Cincinnati      $4.69/ton    Lower Ohio       $4.46/ton
  Cairo-Memphis   $3.14/ton

Lbs per bushel (per AMS GTR methodology, short ton = 2,000 lbs):
  Corn: 56 lbs/bu → 35.714 bu/ton
  Soybeans/Wheat: 60 lbs/bu → 33.333 bu/ton
```

Column names for `fxkn-2w9c` and `8uye-ieij` are discovered at runtime (fetched without `$select`) because they were not verifiable before deployment. Actual column names are logged at INFO level on first fetch so they can be added to the candidate lists in the source.

The unified `fetch_transport_rates()` function returns a normalised list in the transport rate schema that `analysis._build_selling_options()` consumes directly. Rows with `rate_per_bushel=None` (cost-index-only entries) are filtered out in the analysis layer before the selling-options matrix is built.

### nass_client.py

Structurally similar to `ams_client.py` but with an additional concern: the NASS QuickStats free tier allows only 50 requests per day. A module-level list `_nass_request_log` stores timestamps of all NASS requests made in the current process. Before every API call, `_check_nass_rate_limit()` filters that list to the past 24 hours and compares the count to `NASS_DAILY_LIMIT`. If the limit is reached, the function skips the API call and returns an empty dict, triggering the fallback path.

### tools/analysis.py

The most important file in the system. `rank_selling_options()` and `simulate_profit()` are the combination tools that produce the core value — a net-price-per-bushel ranking no farmer could easily produce themselves without a data analyst.

**Key design: independent source fetches with independent fallbacks.** Prices (AMS) and transport rates (Socrata) are fetched sequentially with separate try/except blocks. A failure in one source does not force the other to fall back. If AMS prices are live but Socrata is unreachable, the tool returns live prices against sample transport rates and labels the transport data as fallback — rather than falling back both sources as the old `asyncio.gather()` design did.

`_build_selling_options()` is the core cross-join. It filters transport rows to the origin state and discards any row where `rate_per_bushel` is `None` (index-only entries from the cost indicators dataset). It then iterates every price entry against every usable transport rate, computing `net_price = cash_price - rate_per_bushel`. A seen-set keyed on `(market, mode)` deduplicates combinations. The result is sorted descending by net_price.

`simulate_profit()` applies the same cross-join and then multiplies `net_price × volume_bushels` for each top option. Volume figures are formatted with commas throughout (250,000 not 250000).

### tools/transport.py

Calls `transport_client.fetch_transport_rates()` rather than the old `ams_client.fetch_transport_report()`. Accepts `commodity` as a parameter alongside `farm_location` so the Socrata barge conversion uses the correct bushel weight for corn vs. soybeans vs. wheat. The `mode` filter guards against `None` by normalising with `(r.get("mode") or "").lower()` before comparison. Rows with `rate_per_bushel=None` display as `"N/A (index)"` in the formatted table with an explanatory footnote.

### tools/trends.py

`get_market_trends()` formats a price history table from NASS weekly data with directional indicators and summary statistics (net change, average weekly move, volatility range).

`get_weekly_summary()` converts the same data into a plain-English paragraph. It classifies price movement as rising/falling/flat and as sharp/moderate/slight, then constructs a narrative sentence and a marketing recommendation. The logic is rule-based — no LLM call is made. The output is deterministic given the same input data.

### utils/geo.py

Translates natural language location inputs into two-letter state codes. Resolves four formats: `"City, ST"`, `"City, State"`, city name alone (against a 100-entry lookup table covering major agricultural centers), five-digit ZIP code (against ZIP prefix ranges), and bare state names or abbreviations. Returns `"IA"` as the default fallback for unresolvable inputs, logging a warning. All results are cached for 24 hours.

### dashboard/app.py

A Streamlit application that reads the three log files from `$LOG_DIR` and renders them. It auto-refreshes every 5 seconds. The metrics row shows total calls, success rate, error count, average latency, and security event count. The four tabs cover per-tool performance, individual trace inspection with child span details, the audit event log, and raw server log output. The dashboard has no write access to any part of the system.

---

## Data Layer

### USDA AMS MyMarketNews API

**Base URL:** `https://marsapi.ams.usda.gov/services/v1.2`

Provides grain cash prices reported by elevators and terminals across the US. Reports are identified by slug IDs. The server uses a static registry of confirmed slug IDs rather than querying the `/reports` index at runtime.

**Report structure for grain bid reports:**

```
GET /reports/<slug_id>                         → Report Header section
GET /reports/<slug_id>?section=Report+Detail   → Per-elevator structured rows
```

The Header section contains `report_narrative` text with state-average prices. The Detail section (when available) contains per-elevator structured rows with explicit price fields. The client tries Detail first and falls back to narrative parsing.

**Key fields mapped from AMS response:**

| AMS Field | Internal Field | Notes |
|-----------|---------------|-------|
| `office_city` / `location_name` | `location_name` | Market or elevator name |
| `office_state` / `state` | `state` | Two-letter state code |
| `report_date` / `report_begin_date` | `report_date` | ISO date string |
| Price field (varies by report) | `cash_price` | Float, dollars per bushel |
| Basis field (varies by report) | `basis` | Float, premium/discount vs futures |
| `market_type` / `type` | `market_type` | `terminal`, `elevator`, `processor`, `state_average` |

**API key:** Request at `https://marsapi.ams.usda.gov`. Free tier available. Set as `USDA_AMS_API_KEY`.

### USDA AgTransport Platform (Socrata)

**Base URL:** `https://agtransport.usda.gov/resource`

**Query pattern:** `GET /<dataset_id>.json?$where=...&$order=...&$limit=...&$select=...`

All four datasets used are public. SoQL reference: `https://dev.socrata.com/docs/queries/`

**Confirmed dataset schemas (from live API responses, April 2026):**

`deqi-uken` — Downbound Grain Barge Rates:
```
date, week, month, year, location, rate
```
`rate` = spot/nearby percent of 1976 tariff benchmark.

`fxkn-2w9c` — Quarterly Grain Truck Rates:
```
date, year, quarter, region, [price columns — discovered at runtime]
```

`an4w-mnp7` — Grain Price Spreads:
```
date, week, year, origin, destination, commodity, spread, origin_price, destination_price
```
`spread` = destination_price − origin_price ($/bu). Positive spread = exporting profitable.

`8uye-ieij` — Grain Transportation Cost Indicators:
```
date, week, year, [index columns — discovered at runtime]
```
Index values only. No direct $/bu conversion possible without a base rate.

**Optional app token:** Register at `agtransport.usda.gov`. Set as `SOCRATA_APP_TOKEN`. Without it, anonymous rate limit is 1 request/second.

### USDA NASS QuickStats API

**Base URL:** `https://quickstats.nass.usda.gov/api/api_GET/`

Provides historical agricultural statistics including weekly prices received by farmers.

**Query parameters for weekly corn prices in Iowa:**

```
key=YOUR_KEY
commodity_desc=CORN
statisticcat_desc=PRICE RECEIVED
unit_desc=$ / BU
agg_level_desc=STATE
state_alpha=IA
freq_desc=WEEKLY
format=JSON
```

**Rate limit:** 50 requests/day on free tier. Tracked by `NASS_DAILY_LIMIT`. Set as `USDA_NASS_API_KEY`.

### Data Shape Contract

All internal code expects these normalized dict shapes. Client files are responsible for mapping raw API responses to these shapes before returning.

**Price entry (from ams_client):**
```python
{
    "location_name": str,     # "Des Moines (State Avg)" or "Chicago Terminal"
    "state":         str,     # "IA"
    "market_type":   str,     # "terminal" | "elevator" | "processor" | "state_average"
    "cash_price":    float,   # 4.08  (dollars per bushel)
    "basis":         float,   # 0.0   (0.0 for state averages; actual for elevator rows)
    "report_date":   str,     # "2026-04-08"
    "data_source":   str,     # "USDA AMS (2850)" — include "[SAMPLE]" if fallback
}
```

**Transport rate entry (from transport_client):**
```python
{
    "mode":            str,            # "barge" | "truck" | "rail_or_barge" | "rail"
    "origin_region":   str,            # "IA"
    "destination":     str,            # "Gulf Export via Mid-Mississippi"
    "rate_per_bushel": float | None,   # 0.45 — None for index-only rows
    "note":            str,            # source + context string
    "source_dataset":  str,            # Socrata dataset ID, e.g. "deqi-uken"
}
```

`rate_per_bushel=None` indicates a cost-index-only row (from `8uye-ieij`). The analysis layer filters these out before building selling options. The transport tool displays them as `"N/A (index)"` in formatted output.

**NASS price entry:**
```python
{
    "week_ending":   str,   # "2026-03-01"
    "Value":         str,   # "4.6500" — parse to float when using
    "commodity_desc":str,   # "CORN"
    "state_alpha":   str,   # "IA"
    "unit_desc":     str,   # "$ / BU"
    "source_desc":   str,   # "USDA NASS" — include "[SAMPLE]" if fallback
}
```

---

## Security Design

### Threat Model

The system sits between an LLM client and federal public APIs. The relevant threats are:

**From the client side:** A malicious or jailbroken prompt could attempt to pass injection payloads through tool arguments to manipulate the server's behavior, expose secrets, or cause the server to make unintended requests.

**From the API side:** A compromised or spoofed USDA API response could contain text designed to manipulate the LLM's context when the tool output is returned (context injection through data).

**From the network:** The server makes outbound HTTP calls, making it a potential SSRF vector if URLs are constructed from user input.

### Mitigations by OWASP MCP Top 10

| Risk | Mitigation | Location |
|------|-----------|----------|
| Tool Poisoning | All tools statically registered at startup. No dynamic tool registration from any external input. | `server.py` |
| Secret Exposure | API keys read from environment variables only. Never accepted as tool arguments. Auto-redacted in logs. | `security.py`, `ams_client.py`, `nass_client.py` |
| Prompt Injection | Input sanitization with 10 regex patterns covering known injection vectors. Applied to every string argument. | `security.py` |
| Command Injection | No shell execution anywhere. No subprocess calls. All logic is in-process Python. | Entire codebase |
| Excessive Permissions | Each tool accesses only its declared USDA endpoints. Tools cannot call other tools. No write operations. | `server.py` tool dispatch |
| Insecure Resource Access | All outbound URLs validated against a frozenset of allowed domains before any HTTP call. | `ams_client.py`, `transport_client.py`, `nass_client.py` |
| SSRF | URL allowlist. No user-supplied URLs or URL fragments accepted as arguments. | All client files |
| Context Injection | `sanitize_output()` strips role injection patterns and MCP tool tags from API response data before it reaches the client. | `security.py` |
| Rate Limit Abuse | Token bucket rate limiter at 30 req/min. NASS daily limit tracked separately. Both configurable. | `security.py`, `nass_client.py` |
| Insecure Deserialization | JSON responses subject to 5MB size limit before parsing. Tool schemas use `additionalProperties: false`. | `security.py`, all schemas |

### What Secrets Are in the System

Three secrets exist: `USDA_AMS_API_KEY`, `USDA_NASS_API_KEY`, and optionally `SOCRATA_APP_TOKEN`. The first two are loaded at import time from environment variables, used only as HTTP request headers, never logged, never included in tool output, and actively redacted by `redact_secrets()`. `SOCRATA_APP_TOKEN` is optional — the AgTransport platform is fully public and functional without it; the token only raises the anonymous rate limit.

---

## Observability Design

### Three Log Files

All three files are written to the directory specified by the `LOG_DIR` environment variable, defaulting to `/tmp/agriconnect-logs`.

**`server.log`** — Human-readable structured application log. One line per log call. Format: `[HH:MM:SS] LEVEL logger_name — message`.

**`traces.jsonl`** — One JSON object per line, one line per tool call. Fields: `request_id`, `tool_name`, `start_time`, `duration_ms`, `outcome`, `arg_keys` (not values), `child_spans`, `error`.

**`audit.jsonl`** — Append-only compliance log for tool success/error, rate limit hits, validation errors, and injection detection attempts.

### Trace Span Structure

```python
{
    "request_id":  "rank_selling_options-20260408143215123456",
    "tool_name":   "rank_selling_options",
    "start_time":  "2026-04-08T14:32:15.123456+00:00",
    "duration_ms": 923.4,
    "outcome":     "success",
    "arg_keys":    ["commodity", "farm_location", "radius_miles"],
    "child_spans": [
        { "name": "ams_prices",       "duration_ms": 621.2 },
        { "name": "socrata_transport","duration_ms": 287.4 },
    ]
}
```

Argument keys are logged without values to avoid retaining sensitive business data (farm location, volume) in trace files.

---

## Resilience Design

### HTTP Retry Policy

**AMS client:** Up to 3 attempts, backoff `1.5^attempt` seconds. HTTP 429 reads `Retry-After`. HTTP 4xx fails fast. HTTP 5xx and timeouts retry.

**Transport client (Socrata):** Single attempt per dataset. Socrata is highly available and the data is static/weekly — retries are not warranted. Individual dataset failures are logged and skipped; results from other datasets are still returned.

**NASS client:** Up to 2 attempts, 15-second timeout.

### Graceful Degradation

Prices and transport are fetched with **independent fallback paths**. If AMS fails, prices fall back to labeled sample data while transport continues to fetch from Socrata (or vice versa). The selling-options analysis then runs on whatever combination is available, always labeling fallback sources clearly in the output.

The system never crashes for the end user due to a USDA API outage. It degrades to sample data and clearly says so.

### Rate-Per-Bushel None Filtering

Transport rows from the cost indicators dataset (`8uye-ieij`) have `rate_per_bushel=None` because they provide index values, not usable rates. `_build_selling_options()` filters these out explicitly before building the cross-join matrix. This prevents `None` being cast to `0.0` and producing artificially high net prices.

### NASS Rate Limit Management

A module-level list tracks timestamps of every NASS API call made in the current process. Before each call, entries older than 24 hours are pruned and the count is compared to `NASS_DAILY_LIMIT`. If the limit is reached, the API call is skipped and cached or fallback data is returned.

---

## MCP Integration

### Protocol

The server communicates over stdio using JSON-RPC 2.0 as defined by the MCP specification. The `mcp` Python SDK handles framing, serialization, and the protocol handshake.

### Tool Schema Design

Every tool schema uses `additionalProperties: false`. The `commodity` argument uses an explicit enum allowlist (`["corn", "soybeans", "wheat"]`). The `mode` argument on the transport tool uses `["truck", "rail", "barge", "rail_or_barge"]`. These enums reflect the actual mode values returned by `transport_client.fetch_transport_rates()`.

### Resources

Three resources exposed as MCP resources:

`usda://commodities/supported` — Static JSON list of supported commodities.

`usda://markets/regions` — Static JSON map of USDA reporting regions.

`usda://status` — Live JSON health check with cache statistics, rate limiter state, and server version.

### Discovery

The `mcp.json` manifest at the project root describes the server for MCP client discovery: entry point command, environment variables, tool list, resource list, data sources, security properties, and example queries.

---

## File Structure

```
agriconnect-mcp/
│
├── mcp.json                      # MCP manifest for client discovery
├── README.md                     # This document
├── requirements.txt              # Python dependencies
├── Dockerfile                    # Container image definition
├── docker-compose.yml            # Server + dashboard, one command
├── .env.example                  # Environment variable template
├── claude_desktop_config.json    # Claude Desktop integration snippet
│
├── src/
│   ├── server.py                 # MCP entry point, tool registration, dispatch
│   ├── security.py               # Input sanitization, validation, rate limiting
│   ├── observability.py          # Logging, tracing, audit events
│   ├── cache.py                  # TTL cache with background eviction
│   │
│   ├── clients/
│   │   ├── ams_client.py         # USDA AMS MyMarketNews — grain cash prices
│   │   │                         # Static registry + two-section fetch strategy
│   │   ├── transport_client.py   # USDA AgTransport (Socrata) — transport rates
│   │   │                         # Barge, truck, spreads, cost indices
│   │   └── nass_client.py        # USDA NASS QuickStats — price history
│   │
│   ├── tools/
│   │   ├── prices.py             # get_cash_prices tool
│   │   ├── transport.py          # get_transportation_costs tool
│   │   │                         # Uses transport_client, commodity-aware
│   │   ├── analysis.py           # rank_selling_options, simulate_profit tools
│   │   │                         # Independent fallbacks per data source
│   │   └── trends.py             # get_market_trends, get_weekly_summary tools
│   │
│   └── utils/
│       └── geo.py                # Location string → state code resolution
│
├── dashboard/
│   └── app.py                    # Streamlit observability dashboard
│
└── tests/
    └── test_all.py               # Security, cache, geo, analysis unit tests
```

---

## Configuration Reference

All configuration is via environment variables. No configuration files. No hardcoded values.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `USDA_AMS_API_KEY` | No | `""` | USDA AMS MARS API key. Without it, server runs in demo mode using sample prices. |
| `USDA_NASS_API_KEY` | No | `"DEMO_KEY"` | USDA NASS QuickStats API key. Free tier without it. |
| `SOCRATA_APP_TOKEN` | No | `""` | USDA AgTransport app token. Without it, anonymous rate limit is 1 req/sec. Register free at `agtransport.usda.gov`. |
| `LOG_LEVEL` | No | `"INFO"` | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_DIR` | No | `"/tmp/agriconnect-logs"` | Directory for all log files. Must be writable. |
| `NASS_DAILY_LIMIT` | No | `50` | NASS requests per day before fallback triggers. Increase for paid tier. |

---

## Key Design Decisions

**Why a static slug registry instead of runtime report discovery?**
The AMS `/reports` index returns 1,049 reports with no commodity field — only a `report_title` that uses USDA's own naming conventions (e.g., "Iowa Daily Cash Grain Bids", not "Iowa Corn Prices"). Keyword matching against titles proved unreliable in production. A static registry populated from a one-time exploration of the index is explicit, auditable, fast, and requires no index scan at query time. When USDA adds new reports, the registry is updated in source control.

**Why separate fetches for prices and transport instead of asyncio.gather()?**
`asyncio.gather()` fails atomically — if one coroutine raises, the exception propagates and both results are lost. Since AMS and Socrata are independent systems with independent failure modes, a failure in one should not force sample data in the other. Separate sequential fetches with separate try/except blocks give independent fallback paths while adding only milliseconds of latency (the Socrata barge fetch is ~300ms; the AMS fetch is ~600ms; they overlap well enough that the additional sequential overhead is acceptable).

**Why three separate Socrata datasets instead of one?**
No single AgTransport dataset covers all three modes (barge, truck, rail) with direct $/bushel rates. Barge rates (`deqi-uken`) require a percentage-of-tariff conversion. Truck rates (`fxkn-2w9c`) are quarterly and regional. Price spreads (`an4w-mnp7`) provide the most actionable implied cost for origin-to-export decisions. Combining all three gives the selling-options tool the richest possible transport picture while each dataset fails independently.

**Why are barge benchmark rates hardcoded rather than fetched?**
The 1976 Waterways Freight Bureau Tariff No. 7 benchmarks are a fixed historical reference — they have not changed since 1976 and are documented in every USDA Grain Transportation Report. There is no API that returns them; they are a constant of the barge industry's pricing system. Hardcoding them is correct. The values are sourced directly from USDA AMS GTR publications and the `deqi-uken` dataset description, and cited in the code comments.

**Why plain text tool output instead of JSON?**
MCP tool results go directly into the LLM context. Plain text formatted for readability produces better LLM responses than raw JSON, which the LLM would then need to interpret. The analysis is done in Python before returning; the LLM receives conclusions, not raw data.

**Why are arg values not logged in traces?**
Farm location and commodity volume are potentially sensitive business information. Logging argument keys confirms which tool was called with what parameters (useful for debugging schema issues) without retaining the actual data values.

**Why `additionalProperties: false` on all schemas?**
Any field the server did not explicitly anticipate is rejected at validation time rather than silently ignored. This makes schema drift visible immediately and prevents parameter injection attacks where extra fields might be processed by future code additions that aren't aware they could receive untrusted input.

**Why stdio transport instead of HTTP?**
stdio is simpler, has no port to expose, requires no authentication layer for the transport itself, and is the standard for local MCP deployments. HTTP transport would be appropriate for a hosted multi-tenant version of this server.

**Why in-memory cache instead of Redis?**
Redis adds operational complexity. The data this server caches is small, process-scoped, and acceptable to lose on restart since USDA APIs are the source of truth. For multi-instance deployments, Redis or Memcached would be the correct choice.