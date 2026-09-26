# reinvent-planner

> Score, schedule, and auto-register for AWS re:Invent sessions — from the terminal or through any LLM.

**reinvent-planner** fetches the full re:Invent session catalog, scores every session against your domain interests and learning-level preferences, builds a conflict-free daily schedule that minimises venue-hopping across Las Vegas, and auto-registers for seats the moment they open.

Use it as a **CLI tool**, connect it to **Claude Desktop / Cursor / VS Code** via stdio MCP, or expose it as a **remote MCP server** so Claude.ai, ChatGPT, Gemini, and Perplexity can plan your schedule for you.

---

## Features

| | |
|---|---|
| **Score** | Keyword × AOI × level multiplier gives every session a 0–1 relevance score. Wishlisted sessions lock at 1.0 and are guaranteed a slot. |
| **LLM scoring** | Optional Bedrock re-ranking (Claude Haiku) reorders sessions by semantic relevance. Falls back silently to keyword scoring if unavailable. |
| **Schedule** | Greedy daily optimizer with venue-cluster travel penalties keeps you at the same building as long as possible. |
| **Per-slot approval** | When re-running `plan` against an existing schedule, each changed slot gets an interactive 4-choice prompt — keep current, switch, merge as backup, or discard. |
| **Primary + backup** | Every slot can have a fallback session. `register` and `watch` automatically try the backup if the primary is full or errors. |
| **HTML views** | Self-contained, offline-first HTML for recommendations, conflicts, schedule (day tabs + travel warnings), and registration status. Light/dark mode toggle. |
| **ICS export** | One command exports your schedule to a `.ics` file — import into Google Calendar, Apple Calendar, or Outlook. |
| **Auto-register** | Playwright logs into registration.awsevents.com and clicks Reserve Seat for every session in your schedule. `watch` mode polls until seats open. |
| **Wizard** | `awsevents setup` walks you through every step interactively with console + HTML views at each stage. |
| **MCP server** | Expose all features as MCP tools — locally via stdio or remotely over HTTP for Claude.ai, ChatGPT, Gemini, and Perplexity. |

---

## Quick start

```bash
git clone https://github.com/barroei1981/reinvent-planner.git
cd reinvent-planner
uv sync
playwright install chromium

# Copy and edit your preferences
# (config.yaml is already included — edit email, domains, levels)

echo "AWSEVENTS_PASSWORD=your_password_here" > .env

uv run awsevents sync-wishlist   # scrape catalog + wishlist (requires Chrome)
uv run awsevents plan            # score → schedule → save schedule.json
uv run awsevents watch           # poll until sessions open → auto-register
```

---

## MCP server

Connect any LLM to your re:Invent planner via the [Model Context Protocol](https://modelcontextprotocol.io/).
No Bedrock account required — scoring defaults to keyword matching.

### Tools exposed

| Tool | What it does |
|---|---|
| `list_sessions` | Fetch + score sessions; filter by score, keyword, or day |
| `plan_schedule` | Build an optimised daily schedule and return it as JSON |
| `get_schedule` | Read the saved `schedule.json` |
| `get_config` | Return active preferences from `config.yaml` (credentials excluded) |

### Local clients — Claude Desktop, Cursor, VS Code, Continue

Add to your MCP host's config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "awsevents": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/path/to/reinvent-planner",
        "awsevents-mcp"
      ],
      "env": {
        "AWSEVENTS_CONFIG": "/path/to/reinvent-planner/config.yaml"
      }
    }
  }
}
```

### Remote clients — Claude.ai, ChatGPT, Gemini, Perplexity

Start the HTTP server on any machine (or locally + ngrok):

```bash
# With auth (recommended for public exposure)
AWSEVENTS_API_KEY=your-secret \
  uv run awsevents-mcp --transport http --host 0.0.0.0 --port 8000

# Without auth (trusted network / local only)
uv run awsevents-mcp --transport http --host 127.0.0.1 --port 8000
```

The startup message prints your MCP endpoint (`http://<host>:8000/mcp`).

#### Claude.ai
Settings → Integrations → Add MCP Server → paste the `/mcp` URL.
If auth is enabled, set the `Authorization` header to `Bearer <your-key>`.

#### ChatGPT
Custom GPT → Configure → Add Action → enter the `/mcp` URL.
Add header `Authorization: Bearer <your-key>`.

#### Gemini
Extensions → Custom MCP → enter the `/mcp` URL.

#### Perplexity
Integrations → MCP → enter the `/mcp` URL.

#### Using ngrok for local-to-public exposure

```bash
# Terminal 1
uv run awsevents-mcp --transport http --host 127.0.0.1 --port 8000

# Terminal 2
ngrok http 8000
# Use the https://xxxx.ngrok.io/mcp URL in your LLM host
```

---

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (`brew install uv`)
- Google Chrome with a signed-in Profile 1 (used to read auth cookies — no password stored)
- An AWS Builder ID / re:Invent registration account

---

## Installation

### Requirements

| Requirement | Notes |
|---|---|
| Python 3.10+ | Check: `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | `brew install uv` or `curl -Ls https://astral.sh/uv/install.sh \| sh` |
| Google Chrome | With a signed-in profile (for wishlist scraping + registration) |
| AWS Builder ID | Free account at [builderid.aws](https://profile.aws.amazon.com/) |

### Steps

```bash
# 1. Clone the repo
git clone https://github.com/barroei1981/reinvent-planner.git
cd reinvent-planner

# 2. Install Python dependencies
uv sync

# 3. Install the Chromium browser used for automation
uv run playwright install chromium

# 4. Set your re:Invent password (gitignored — never committed)
echo "AWSEVENTS_PASSWORD=your_password_here" > .env

# 5. Edit config.yaml with your email and preferences (see below)
```

That's it. Run `uv run awsevents --help` to verify the install.

### Installing as an MCP server (for LLM use)

The MCP dependency is optional — install it only if you want LLM integration:

```bash
uv sync --extra mcp
```

Then follow the [MCP server section](#mcp-server) below.

---

## Configuration

Edit `config.yaml` directly — it ships with sensible defaults for a GenAI/ML-focused attendee:

```yaml
registration:
  email: "your@email.com"    # your AWS Builder ID email
  # Password comes from .env (AWSEVENTS_PASSWORD)

preferences:
  areas_of_interest:
    - "Generative AI"
    - "Agentic AI"
    - "Machine Learning"
  levels:
    preferred: ["Advanced", "Expert"]
    acceptable: ["Intermediate"]
    avoid: ["Foundational"]
  max_sessions_per_day: 6
  buffer_minutes: 15          # gap to leave between sessions for travel
```

See `config.yaml` for the full reference — domain keyword weights, session types, venue clusters, schedule optimizer settings, and more.

### (Optional) Enable Bedrock LLM scoring

If you have AWS credentials with `bedrock:InvokeModel` access, Claude Haiku can re-rank sessions by semantic relevance instead of pure keyword matching:

```yaml
# config.yaml
llm:
  enabled: true
  model_id: "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
  region: "eu-west-1"
  aws_profile: ""   # leave blank — reads AWS_PROFILE from .env
```

```bash
echo "AWS_PROFILE=your-sso-profile" >> .env
```

Fallback to keyword scoring is automatic and silent if Bedrock is unavailable.

---

## Usage

### Interactive wizard (recommended for first run)

```bash
uv run awsevents setup
```

Walks through: user details → interests → fetch catalog → review recommendations → resolve conflicts → view schedule → register/watch → post-registration status.

### Individual commands

```bash
# Fetch the full re:Invent catalog + your wishlist (requires Chrome + auth cookies)
uv run awsevents sync-wishlist

# Score sessions and build an optimised schedule → saves schedule.json
uv run awsevents plan

# Show schedule day-by-hour in the terminal (+ optional HTML view)
uv run awsevents schedule

# Export schedule to a calendar file
uv run awsevents export-ics              # writes reinvent2026.ics

# Register immediately for all sessions in schedule.json
uv run awsevents register

# Poll until sessions open, then auto-register
uv run awsevents watch

# Swap a session in your schedule with a recommended alternative
uv run awsevents edit --day 2026-12-03 --time 10:30
```

---

## How scoring works

Each session gets a score from 0 to 1:

1. **Keyword match** — title + description checked against your `domains` keywords, weighted by domain priority
2. **AOI bonus** — sessions whose `areas_of_interest` facet matches your config get a +20% boost
3. **Level multiplier** — `preferred` levels (e.g. Advanced/Expert) keep their score; `acceptable` × 0.6; `avoid` × 0.2
4. **Wishlisted sessions** — locked at 1.0 regardless of keywords; always included in the schedule
5. **Bedrock re-ranking** (optional) — Claude Haiku re-scores sessions in batches of 20; wishlisted sessions stay locked at 1.0

---

## How the scheduler works

Given scored sessions, the greedy optimizer:

1. Groups sessions by day
2. Picks the highest-scoring session that fits in the remaining day
3. Applies a **venue-hop penalty** when the next pick requires a different cluster — biases toward staying in the same building
4. Respects `max_sessions_per_day` and `buffer_minutes` between sessions

### Re-planning with an existing schedule

Running `plan` against an existing `schedule.json` triggers a per-slot approval gate. For each slot where the new plan recommends a different session:

```
⚡  Wed Dec 2  13:00
  Current : How Prime Video Built an AI-assisted Creative Suite on AWS
           score 0.82  Venetian / Level 3
  Bedrock : Agentic AI Governance for Regulated Industries
           score 0.91  Caesars Forum
  (1) Keep current as primary  +  add Bedrock as backup
  (2) Switch to Bedrock primary  +  keep current as backup
  (3) Keep only current  (ignore Bedrock)
  (4) Take only Bedrock  (drop current)
  Choice [1]:
```

New slots and dropped slots get a simple yes/no prompt. Existing backup sessions are always preserved.

### Primary + backup registration

Each slot can have a backup session. If `register` or `watch` cannot secure the primary (full seat, error, or not yet open after max retries), it automatically tries the backup:

```
  [full] Emerging multi-agent patterns for complex financial workflows
  → primary full — trying backup: Evaluating AI agents for production in ...
  [registered] Evaluating AI agents for production in financial services
```

---

## HTML views

Each HTML output is a **single self-contained file** — no CDN, works offline, safe to open from `file://`.

| Command / step | Output file |
|---|---|
| `setup` step 3 | recommendations with score bars + exclude checkboxes |
| `setup` step 4 | side-by-side conflict cards |
| `setup` step 5 / `awsevents schedule` | day-tabs schedule with travel warnings |
| `setup` step 7 | registration status grid + gaps analysis |
| `awsevents edit --html` | slot alternatives for a specific time window |

All views use the **Glass** design: frosted cards, gradient background, Geist typography, venue cluster colors (indigo / pink / teal / amber), light + dark mode. Backup sessions appear with an amber dashed border and a `⚑ BACKUP` badge.

---

## Schedule view

Day-tabs across the top. Venue cluster color-coded left bar on every card. Travel warnings between venue changes. Keynote blocks in purple. Light/dark mode persists to localStorage.

```
reinvent-planner  Mon Dec 1  Tue Dec 2  Wed Dec 3  Thu Dec 4   ☀
──────────────────────────────────────────────────────────────────
Monday, Dec 1                                    7 sessions
─────────────────────────────────────────────────────────────────
11:00   ▌  Build It, Govern It, Ship It…          Advanced  ↗
12:00   ▌  AWSome Automation: From Brief to Page  Advanced  ↗
        ⚠  Venetian/Wynn → Caesars · 15 min travel · 0 min gap
13:00   ▌  Observe AI agents beyond your perimeter
...
```

---

## Project structure

```
agent/
  catalog.py      — public AWS Events API fallback fetcher
  wishlist.py     — catalog.awsevents.com REST + Playwright scraper
  scorer.py       — keyword × AOI × level scoring
  llm_scorer.py   — optional Bedrock re-ranking (wraps scorer.py as fallback)
  scheduler.py    — greedy daily optimizer with venue-hop penalty
  registrar.py    — Playwright auto-registration (primary + backup flow)
  wizard.py       — 7-step interactive setup wizard
  html_views.py   — self-contained HTML view generator (Glass design)
  main.py         — CLI entry point (click) — plan / list / register / watch
  mcp_server.py   — MCP server (stdio + HTTP transports)
config.yaml       — all user preferences, no credentials
.env              — AWSEVENTS_PASSWORD + AWS_PROFILE (gitignored)
```

---

## Security notes

- **Password** — stored only in `.env` (gitignored). Never committed.
- **Email** — stored in `config.yaml`. Replace with your own before use.
- **AWS profile** — stored only in `.env`. Never committed.
- **MCP API key** — set `AWSEVENTS_API_KEY` in the environment. Never committed. The `/mcp` endpoint returns 401 for any request without a valid Bearer token when the key is set.
- **Catalog API IDs** — Rainfocus widget IDs embedded in the public catalog page. Configurable in `config.yaml` under `catalog_api`.
- **Cookies** — read locally from Chrome Profile 1 via `browser_cookie3`. Not stored, not transmitted anywhere except to `catalog.awsevents.com`.

---

## License

MIT
