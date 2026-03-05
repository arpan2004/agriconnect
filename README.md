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

AgriConnect MCP is a Model Context Protocol server that exposes USDA commodity data through structured tools that any MCP-compatible AI client can call. It bridges two USDA data sources — AMS (Agricultural Marketing Service) and NASS (National Agricultural Statistics Service) — and adds an analysis layer that combines price and transportation cost data to produce ranked selling recommendations.

The server is designed around five properties: correctness of data, security of inputs and outputs, observability of every operation, resilience to external API failure, and full compliance with the MCP specification.

**Primary use case:** A farmer asks a natural language question like *"Where should I sell 25,000 bushels of corn from Ames, Iowa?"* The MCP client routes that question to this server, which fetches live USDA data, cross-joins prices against transportation costs, and returns a ranked list of selling locations with net profit per bushel.

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
│  prices.py      get_cash_prices()         → price table             │
│  transport.py   get_transportation_costs() → rate table             │
│  analysis.py    rank_selling_options()    → ranked net profit list  │
│                 simulate_profit()         → total revenue table     │
│  trends.py      get_market_trends()       → price history table     │
│                 get_weekly_summary()      → narrative paragraph     │
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
│    Cash prices       15 minutes    (USDA updates ~twice/day)         │
│    Transport rates    6 hours      (weekly report, stable intraday)  │
│    NASS history       1 hour       (weekly publication cadence)      │
│    Geo lookups       24 hours      (static reference data)           │
│    Health checks     30 seconds                                      │
│                                                                      │
│  Max 500 entries. LRU eviction at capacity. Background sweep        │
│  every 5 minutes to expire stale entries.                            │
└──────────┬───────────────────────────────────────────────────────────┘
           │  cache miss only
           │
           ├──────────────────────────┐
           ▼                          ▼
┌────────────────────┐    ┌─────────────────────────┐
│   ams_client.py    │    │     nass_client.py       │
│                    │    │                          │
│  fetch_grain_      │    │  fetch_commodity_        │
│  prices()          │    │  prices()                │
│                    │    │                          │
│  fetch_transport_  │    │  Daily rate limit        │
│  report()          │    │  tracker (50 req/day     │
│                    │    │  on free NASS tier)      │
│  Retry: 3×         │    │  Retry: 2×               │
│  Backoff: 1.5^n s  │    │  Timeout: 15s            │
│  Timeout: 12s      │    │                          │
│  URL allowlist     │    │  URL allowlist           │
└──────────┬─────────┘    └───────────┬──────────────┘
           │                          │
           ▼                          ▼
┌────────────────────┐    ┌─────────────────────────┐
│   USDA AMS API     │    │   USDA NASS QuickStats   │
│                    │    │                          │
│  marsapi.ams       │    │  quickstats.nass         │
│  .usda.gov         │    │  .usda.gov/api           │
│                    │    │                          │
│  Cash grain prices │    │  Weekly prices received  │
│  Transport report  │    │  Production statistics   │
└────────────────────┘    └─────────────────────────┘
```

---

## Request Lifecycle

This is the exact sequence of operations for every tool call, from client query to response.

```
1.  MCP client sends tool call request
      { name: "rank_selling_options", arguments: { commodity: "corn", farm_location: "Ames, IA" } }

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

7.  Tool function dispatched: rank_selling_options(commodity="corn", farm_location="Ames, IA")

8.  geo.resolve_location("Ames, IA") → "IA"
      → Cache check: HIT → return "IA" immediately
      → Cache miss: resolve from lookup table, write to cache (TTL 24h)

9.  fetch_grain_prices("corn", "IA") called concurrently with fetch_transport_report()
      Both check cache first

      For each:
        → Cache HIT: return immediately, no API call
        → Cache MISS:
            a. Validate URL against allowlist (*.usda.gov only)
            b. Build request with API key from environment (never from args)
            c. Execute HTTP GET with 12s timeout
            d. On 429: wait Retry-After seconds, retry
            e. On 5xx: exponential backoff (1.5^n), retry up to 3×
            f. On 4xx: fail fast, no retry
            g. On success: parse JSON, check size limit (5MB max), write to cache
            h. On all retries exhausted: return labeled fallback data

10. _build_selling_options() cross-joins prices × transport rates
      net_price = cash_price - transport_cost
      Deduplicate by (market, mode) pair
      Sort descending by net_price

11. Format result as plain text string

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

`RateLimiter` is a thread-safe token bucket. It maintains a list of request timestamps and counts only those within a 60-second rolling window. At 30 requests the next call returns False without acquiring a lock — the check is cheap. Status is exposed via the health resource endpoint.

`redact_secrets()` scans any text for patterns matching API key, token, secret, password, and bearer credential formats, and replaces matched values with `[REDACTED]`. This runs on log output to ensure keys set in environment variables never appear in log files even if accidentally included in an error message.

`sanitize_output()` runs on the formatted string before it returns to the MCP client. It strips role injection patterns (`system:`, `user:`, `assistant:` at line start) and any MCP tool call tags that a malicious or compromised API response might include to hijack the client's next action.

### cache.py

A TTL cache backed by a Python dictionary. Every entry stores a tuple of `(value, expires_at)` where `expires_at` is a Unix timestamp. On `get()`, if the current time exceeds `expires_at`, the entry is deleted and None is returned. On `set()`, if the store is at its 500-entry capacity, the entry with the earliest expiry is evicted first.

A daemon thread runs every 5 minutes and calls `_evict_expired()` to prevent unbounded memory accumulation in long-running deployments. The thread is daemon-flagged so it does not block process shutdown.

Cache keys are SHA-256 hashes of the URL plus sorted parameter dict, truncated to 32 hex characters. This means the same logical query always hits the same cache entry regardless of parameter ordering.

### ams_client.py

Handles all communication with USDA AMS. The core function `_fetch_with_retry()` is shared by both `fetch_grain_prices()` and `fetch_transport_report()`. It validates the URL against the domain allowlist before making any network call. The API key is read from the environment variable `USDA_AMS_API_KEY` and added as a Bearer header — it is never accepted as a function argument and never appears in logs.

Retry logic: up to 3 attempts with backoff of `1.5^attempt` seconds. HTTP 429 responses read the `Retry-After` header and sleep that duration. HTTP 4xx responses (except 429) fail fast with no retry since a client error will not improve. HTTP 5xx and timeouts are retried.

When all retries are exhausted, the function raises `ConnectionError`. The tool layer catches this and returns the fallback dataset clearly labeled as sample data. The fallback exists so the server remains functional for demonstration when USDA APIs are unavailable — it is not a substitute for live data in production.

### nass_client.py

Structurally similar to `ams_client.py` but with an additional concern: the NASS QuickStats free tier allows only 50 requests per day. A module-level list `_nass_request_log` stores timestamps of all NASS requests made in the current process. Before every API call, `_check_nass_rate_limit()` filters that list to the past 24 hours and compares the count to `NASS_DAILY_LIMIT`. If the limit is reached, the function skips the API call and returns an empty dict, which triggers the fallback path. This limit is configurable via the `NASS_DAILY_LIMIT` environment variable for deployments with paid API access.

### tools/analysis.py

The most important file in the system. `rank_selling_options()` and `simulate_profit()` are the tools that justify the server's existence — they are the combination logic that a farmer has no way to do themselves without a data analyst.

`_build_selling_options()` is the core function. It takes the list of price entries and the transport report dict, filters transport rates to the farm's origin state, then iterates every price entry against every transport rate, computing `net_price = cash_price - transport_cost`. A seen-set keyed on `(market, mode)` deduplicates combinations. The result is a flat list of `SellingOption` dataclasses that the calling tool sorts descending by net_price.

The two API calls — prices and transport — are made concurrently with `asyncio.gather()`. If either returns empty, the function falls back gracefully rather than crashing.

### tools/trends.py

`get_market_trends()` formats a price history table from NASS weekly data with directional indicators and summary statistics (net change, average weekly move, volatility range).

`get_weekly_summary()` converts the same data into a plain-English paragraph. It classifies price movement as rising/falling/flat and as sharp/moderate/slight, then constructs a narrative sentence and a marketing recommendation. The logic is rule-based — no LLM call is made. The output is deterministic given the same input data.

### utils/geo.py

Translates natural language location inputs into two-letter state codes that USDA APIs accept. Resolves four formats: `"City, ST"`, `"City, State"`, city name alone (against a 100-entry lookup table covering major agricultural centers), five-digit ZIP code (against ZIP prefix ranges), and bare state names or abbreviations. Returns `"IA"` as the default fallback for unresolvable inputs, logging a warning. All results are cached for 24 hours.

### dashboard/app.py

A Streamlit application that reads the three log files from `$LOG_DIR` and renders them. It auto-refreshes every 5 seconds using Streamlit's rerun mechanism. The metrics row shows total calls, success rate, error count, average latency, and security event count. The four tabs cover per-tool performance tables and bar charts, individual trace inspection with child span details, the audit event log with security events expanded by default, and raw server log output.

The dashboard has no write access to any part of the system. It reads log files only.

---

## Data Layer

### USDA AMS MARS API

**Base URL:** `https://marsapi.ams.usda.gov/services/v1.2`

Provides grain cash prices reported by elevators and terminals across the US. The report catalog is browsable at the base URL. The relevant reports are daily grain price reports identified by slug codes (e.g., `SJ_GR110`). Responses are JSON with a `results` array.

**Key fields to map from AMS response:**

| AMS Field | Internal Field | Notes |
|-----------|---------------|-------|
| `location_name` or similar | `location_name` | Market or elevator name |
| `state` | `state` | Two-letter state code |
| `report_date` | `report_date` | ISO date string |
| Cash price field | `cash_price` | Float, dollars per bushel |
| Basis field | `basis` | Float, premium/discount vs futures |
| Office/type field | `market_type` | `terminal`, `elevator`, `processor` |

**Transport report:** Published weekly as a text file at `https://www.ams.usda.gov/mnreports/sj_gr225.txt`. This is not a JSON API — it requires a text parser. The file uses a fixed-width or tab-separated structure with sections for truck, rail, and barge rates by origin region and destination.

**API key:** Request at `https://marsapi.ams.usda.gov`. Free tier available.

### USDA NASS QuickStats API

**Base URL:** `https://quickstats.nass.usda.gov/api/api_GET/`

Provides historical agricultural statistics including weekly prices received by farmers by state and commodity.

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

**Key fields in NASS response:**

| NASS Field | Notes |
|-----------|-------|
| `Value` | Price as string, parse to float |
| `week_ending` | ISO date of the week ending |
| `state_alpha` | Two-letter state code |
| `commodity_desc` | Commodity name |
| `unit_desc` | Should be `$ / BU` |

**Rate limit:** 50 requests/day on free tier. Paid access available. The `NASS_DAILY_LIMIT` environment variable controls the tracked limit.

**API key:** Request at `https://quickstats.nass.usda.gov/api`. Typically instant approval.

### Data Shape Contract

All internal code expects these normalized dict shapes after parsing. The client files are responsible for mapping raw API responses to these shapes before returning.

**Price entry:**
```python
{
    "location_name": str,     # "Chicago"
    "state":         str,     # "IL"
    "market_type":   str,     # "terminal" | "elevator" | "processor"
    "cash_price":    float,   # 4.9400
    "basis":         float,   # 0.12 (positive = premium, negative = discount)
    "report_date":   str,     # "2026-03-01"
    "data_source":   str,     # "USDA AMS" — include "[SAMPLE]" if fallback
}
```

**Transport rate entry:**
```python
{
    "mode":              str,   # "truck" | "rail" | "barge"
    "origin_region":     str,   # "IA" (state code)
    "destination":       str,   # "Chicago IL"
    "rate_per_bushel":   float, # 0.28
    "note":              str,   # optional, e.g. "Mississippi River route"
}
```

**NASS price entry:**
```python
{
    "week_ending":        str,   # "2026-03-01"
    "Value":              str,   # "4.6500" — parse to float when using
    "commodity_desc":     str,   # "CORN"
    "state_alpha":        str,   # "IA"
    "unit_desc":          str,   # "$ / BU"
    "source_desc":        str,   # "USDA NASS" — include "[SAMPLE]" if fallback
}
```

If the actual USDA API response fields differ from these shapes, the fix belongs in the client file (`ams_client.py` or `nass_client.py`). Nothing above the client layer should need to change when the raw API response shape changes.

---

## Security Design

### Threat Model

The system sits between an LLM client and federal public APIs. The relevant threats are:

**From the client side:** A malicious or jailbroken prompt could attempt to pass injection payloads through tool arguments to manipulate the server's behavior, expose secrets, or cause the server to make unintended requests.

**From the API side:** A compromised or spoofed USDA API response could contain text designed to manipulate the LLM's context when the tool output is returned (context injection / prompt injection through data).

**From the network:** The server makes outbound HTTP calls, making it a potential SSRF vector if URLs are constructed from user input.

### Mitigations by OWASP MCP Top 10

| Risk | Mitigation | Location |
|------|-----------|----------|
| Tool Poisoning | All tools statically registered at startup. No dynamic tool registration from any external input. | `server.py` |
| Secret Exposure | API keys read from environment variables only. Never accepted as tool arguments. Auto-redacted in logs. | `security.py`, `ams_client.py`, `nass_client.py` |
| Prompt Injection | Input sanitization with 10 regex patterns covering known injection vectors. Applied to every string argument. | `security.py` |
| Command Injection | No shell execution anywhere. No subprocess calls. All logic is in-process Python. | Entire codebase |
| Excessive Permissions | Each tool accesses only its declared USDA endpoints. Tools cannot call other tools. No write operations. | `server.py` tool dispatch |
| Insecure Resource Access | All outbound URLs validated against a frozenset of allowed domains before any HTTP call. | `ams_client.py`, `nass_client.py` |
| SSRF | URL allowlist. No user-supplied URLs or URL fragments accepted as arguments. | `ams_client.py`, `nass_client.py` |
| Context Injection | `sanitize_output()` strips role injection patterns and MCP tool tags from API response data before it reaches the client. | `security.py` |
| Rate Limit Abuse | Token bucket rate limiter at 30 req/min. NASS daily limit tracked separately. Both configurable. | `security.py`, `nass_client.py` |
| Insecure Deserialization | JSON responses subject to 5MB size limit before parsing. Tool schemas use `additionalProperties: false`. | `security.py`, all schemas |

### What Secrets Are in the System

Two secrets exist: `USDA_AMS_API_KEY` and `USDA_NASS_API_KEY`. Both are loaded at import time from environment variables. They are used only as HTTP request headers. They are never logged, never included in tool output, never accepted as function arguments, and actively redacted by `redact_secrets()` if they somehow appear in any string that passes through the logging layer.

The `.env.example` file documents where these go. The `.env` file is in `.gitignore`.

---

## Observability Design

### Three Log Files

All three files are written to the directory specified by the `LOG_DIR` environment variable, defaulting to `/tmp/agriconnect-logs`.

**`server.log`** — Human-readable structured application log. One line per log call. Format: `[HH:MM:SS] LEVEL logger_name — message`. Also written as JSON in parallel for machine parsing. Captures info, warning, and error events from all modules.

**`traces.jsonl`** — One JSON object per line, one line per tool call. Written by `ToolCallSpan.finish()` when the trace context manager exits. Fields: `request_id`, `tool_name`, `start_time`, `duration_ms`, `outcome`, `arg_keys` (not values — values are not logged), `child_spans`, `error` (if outcome is error).

**`audit.jsonl`** — Append-only compliance log. Written by `log_audit_event()` for tool success, tool error, rate limit hits, validation errors, injection detection attempts, and unexpected argument fields. Secrets are scrubbed from metadata before writing. This file is the forensic record — it should never be truncated in production.

### Trace Span Structure

```python
{
    "request_id":   "rank_selling_options-20260301143215123456",
    "tool_name":    "rank_selling_options",
    "start_time":   "2026-03-01T14:32:15.123456+00:00",
    "duration_ms":  847.3,
    "outcome":      "success",
    "arg_keys":     ["commodity", "farm_location", "radius_miles"],
    "child_spans":  [
        { "name": "cache_check_prices", "duration_ms": 0.4, "result": "miss" },
        { "name": "ams_api_fetch",       "duration_ms": 612.1 },
        { "name": "cache_check_transport", "duration_ms": 0.3, "result": "hit" }
    ]
}
```

Argument keys are logged without values. This prevents sensitive or PII-adjacent data (farm location, volume) from appearing in trace files while still allowing debugging of which arguments were passed.

### Dashboard

The Streamlit dashboard at `http://localhost:8501` reads all three log files and renders them in four tabs: tool performance summary with bar charts, individual trace inspector, audit event log with security alerts highlighted, and raw server log. It has no write access to anything. It auto-refreshes every 5 seconds.

---

## Resilience Design

### HTTP Retry Policy

All USDA API calls go through `_fetch_with_retry()` in each client file. The policy is:

- Maximum 3 attempts for AMS, 2 for NASS
- Exponential backoff: `1.5^attempt` seconds between retries
- HTTP 429: read `Retry-After` header, sleep that duration, then retry
- HTTP 4xx (except 429): fail immediately, do not retry (client error will not improve)
- HTTP 5xx: retry with backoff
- Timeout (`httpx.TimeoutException`): retry with backoff
- All retries exhausted: raise `ConnectionError` with last error message

### Graceful Degradation

When `ConnectionError` is raised from a client, the tool layer catches it and returns the fallback dataset. The fallback is structurally identical to live data so the rest of the pipeline — analysis, formatting, ranking — operates normally. All fallback responses include `[SAMPLE]` in the `data_source` field, and the formatted output includes an explicit warning to the user.

The system never crashes for the end user due to a USDA API outage. It degrades to sample data and clearly says so.

### Context Window Protection

Tools do not return unbounded data to the LLM. Price tables are sorted and the top entries are returned. If USDA APIs return large result sets, the tool formats only the most relevant entries. The analysis tool returns a fixed-width ranked list regardless of how many market combinations exist. This prevents context window overflow for large geographic queries.

### NASS Rate Limit Management

A module-level list tracks the timestamp of every NASS API call made in the current process lifetime. Before each call, entries older than 24 hours are pruned and the remaining count is compared to `NASS_DAILY_LIMIT`. If the limit is reached, the API call is skipped and the cached result (if any) or fallback is returned. This is a best-effort tracker — it resets if the process restarts, and it does not coordinate across multiple server instances.

---

## MCP Integration

### Protocol

The server communicates over stdio using JSON-RPC 2.0 as defined by the MCP specification. The `mcp` Python SDK handles framing, serialization, and the protocol handshake. The server declares its capabilities (tools, resources, prompts) during initialization.

### Tool Schema Design

Every tool schema uses `additionalProperties: false` to prevent parameter pollution. Required fields are explicitly declared. Enum fields use explicit allowlists rather than open strings for commodity names and transport modes. This means the server rejects any argument combination not explicitly anticipated at design time, which is the correct behavior for a server that makes real API calls.

Tool descriptions are written to be useful to the LLM doing tool selection, not to humans. They describe what the tool returns and when to use it, not how it works internally.

### Resources

Three resources are exposed as MCP resources (readable without a tool call):

`usda://commodities/supported` — Static JSON list of supported commodities with USDA codes. Useful for clients that want to validate commodity names before calling a tool.

`usda://markets/regions` — Static JSON map of USDA reporting regions and their major markets. Useful for understanding geographic coverage.

`usda://status` — Live JSON health check. Returns cache statistics, rate limiter state, and server version. Refreshed on every read.

### Prompts

Three prompt templates are registered with the MCP server. These are pre-filled query strings that MCP clients can surface to users as quick-start options. They cover the farmer selling decision scenario, a market overview request, and a transportation cost comparison. Arguments are sanitized with `sanitize_input()` before interpolation.

### Discovery

The `mcp.json` manifest file at the project root describes the server for MCP client discovery: entry point command, environment variables, tool list, resource list, data sources, security properties, and example queries. This file is what a user copies into their Claude Desktop or other MCP client configuration.

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
│   │   ├── ams_client.py         # USDA AMS API — prices + transport
│   │   └── nass_client.py        # USDA NASS QuickStats API — history
│   │
│   ├── tools/
│   │   ├── prices.py             # get_cash_prices tool
│   │   ├── transport.py          # get_transportation_costs tool
│   │   ├── analysis.py           # rank_selling_options, simulate_profit tools
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
| `USDA_AMS_API_KEY` | No | `""` | USDA AMS MARS API key. Without it, server runs in demo mode. |
| `USDA_NASS_API_KEY` | No | `"DEMO_KEY"` | USDA NASS QuickStats API key. Free tier without it. |
| `LOG_LEVEL` | No | `"INFO"` | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_DIR` | No | `"/tmp/agriconnect-logs"` | Directory for all log files. Must be writable. |
| `NASS_DAILY_LIMIT` | No | `50` | NASS requests per day before fallback triggers. Increase for paid tier. |

---

## Key Design Decisions

**Why stdio transport instead of HTTP?** The MCP specification supports both. stdio is simpler, has no port to expose, requires no authentication layer for the transport itself, and is the standard for local MCP deployments. HTTP transport would be appropriate for a hosted multi-tenant version of this server.

**Why in-memory cache instead of Redis?** Redis adds operational complexity (another process to run, network dependency). The data this server caches is small, process-scoped, and acceptable to lose on restart since USDA APIs are the source of truth. For multi-instance deployments, Redis or Memcached would be the correct choice.

**Why fallback data instead of hard failure?** The primary use case is demonstration and development. Hard failure when USDA APIs are unavailable would make the server unusable for most of the hackathon build cycle. The fallback path exercises the entire pipeline — security, analysis, formatting, ranking — with realistic data. In production the fallback should be replaced with a clear error and a cache-only mode that returns the last known good data.

**Why plain text tool output instead of JSON?** MCP tool results go directly into the LLM context. Plain text formatted for readability produces better LLM responses than raw JSON, which the LLM would then need to interpret. The analysis is done in Python before returning; the LLM receives conclusions, not raw data.

**Why are arg values not logged in traces?** Farm location and commodity volume are potentially sensitive business information. Logging argument keys confirms which tool was called with what parameters (useful for debugging schema issues) without retaining the actual data values. Audit log entries contain only metadata about the call outcome, not the query content.

**Why `additionalProperties: false` on all schemas?** Any field the server did not explicitly anticipate is rejected at validation time rather than silently ignored. This makes schema drift visible immediately and prevents parameter injection attacks where extra fields might be processed by future code additions that aren't aware they could receive untrusted input.