# awsevents_agent

## Project Overview
re:Invent 2026 session planner agent. Fetches the public AWS Events catalog,
scores sessions by keyword-based domain relevance and learning level, builds an
optimised daily schedule that minimises venue-hopping across Las Vegas buildings,
and auto-registers for sessions via Playwright once seats open.

## Stack
- Python 3.10+ / uv
- httpx (catalog API calls)
- Playwright/Chromium (registration automation)
- pydantic, pyyaml, click, rich
- Source: `aws-samples/sample-smb-solutions/aws-events-mcp` catalog endpoint

## Key Files
- `config.yaml`         — user preferences (domains, level, dates, credentials)
- `agent/catalog.py`    — async catalog fetcher (calls AWS content-directory API)
- `agent/scorer.py`     — keyword domain scorer + level multiplier
- `agent/scheduler.py`  — greedy daily schedule optimizer with venue-hop penalty
- `agent/registrar.py`  — Playwright login + seat auto-registration
- `agent/main.py`       — CLI: plan / list / register / watch
- `agent/mcp_server.py` — MCP server (4 tools: list_sessions, plan_schedule, get_schedule, get_config)

## Usage
```
uv run awsevents plan          # fetch → score → schedule → save schedule.json
uv run awsevents list          # show all matching sessions with scores
uv run awsevents register      # register from schedule.json immediately
uv run awsevents watch         # poll until sessions open, then auto-register
uv run awsevents-mcp           # start MCP server (stdio, for Claude Desktop / Cursor / etc.)
```

## MCP Server
Add to `claude_desktop_config.json` (or equivalent host config):
```json
{
  "mcpServers": {
    "awsevents": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/awsevents_agent", "awsevents-mcp"],
      "env": { "AWSEVENTS_CONFIG": "/path/to/awsevents_agent/config.yaml" }
    }
  }
}
```
Scoring is keyword-based by default. Set `llm.enabled: true` in config.yaml to
opt into Bedrock re-ranking (requires AWS credentials; falls back silently).

## LCH Harness
This project uses the LCH Harness. Key paths:
- PRD: `docs/planning-artifacts/prd.md`
- Architecture: `docs/planning-artifacts/architecture.md`
- Story status: `docs/implementation-artifacts/sprint-status.yaml`
- Story files: `docs/implementation-artifacts/{story-key}.md`
- Memory: `.harness/memory.md`
- Decisions: `.harness/decisions/`

## Conventions
- Async-first: all I/O in async functions, called via asyncio.run() at CLI edge
- Config-driven: no hardcoded domains, levels, or dates — everything in config.yaml
- Credentials via .env only: AWSEVENTS_PASSWORD (never committed)

## Infrastructure Constraints
- No AWS credentials required — catalog endpoint is public
- Playwright Chromium installed via: `playwright install chromium`
