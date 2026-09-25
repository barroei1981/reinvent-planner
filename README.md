# reinvent-planner

> Score, schedule, and auto-register for AWS re:Invent sessions — all from the terminal.

**reinvent-planner** fetches the full re:Invent session catalog, scores every session against your domain interests and learning-level preferences, builds a conflict-free daily schedule that minimises venue-hopping across Las Vegas, and auto-registers for seats the moment they open.

---

## Features

| | |
|---|---|
| **Score** | Keyword × AOI × level multiplier gives every session a 0–1 relevance score. Wishlisted sessions lock at 1.0 and are guaranteed a slot. |
| **Schedule** | Greedy daily optimizer with venue-cluster travel penalties keeps you at the same building as long as possible. |
| **HTML views** | Self-contained, offline-first HTML for recommendations, conflicts, schedule (day tabs + travel warnings), and registration status. Light/dark mode toggle. |
| **ICS export** | One command exports your schedule to a `.ics` file — import into Google Calendar, Apple Calendar, or Outlook. |
| **Auto-register** | Playwright logs into registration.awsevents.com and clicks Reserve Seat for every session in your schedule. `watch` mode polls until seats open. |
| **Wizard** | `awsevents setup` walks you through every step interactively with console + HTML views at each stage. |

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

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (`brew install uv`)
- Google Chrome with a signed-in Profile 1 (used to read auth cookies — no password stored)
- An AWS Builder ID / re:Invent registration account

---

## Setup

### 1. Install

```bash
git clone https://github.com/barroei1981/reinvent-planner.git
cd reinvent-planner
uv sync
playwright install chromium
```

### 2. Configure

Copy the example config and fill in your details:

```bash
cp config.yaml config.yaml   # already included — edit directly
```

Key fields in `config.yaml`:

```yaml
registration:
  email: "your@email.com"    # your AWS Builder ID email

catalog_api:
  rf_profile_id: "..."       # Rainfocus widget ID — see config.yaml comments
  rf_widget_id: "..."        # for how to find these in DevTools

preferences:
  areas_of_interest:
    - "Generative AI"
    - "Agentic AI"
    - "Machine Learning"
  levels:
    preferred: ["Advanced", "Expert"]
```

See `config.yaml` for the full reference — domains, keywords, session types, venue clusters, and schedule optimizer settings are all configurable.

### 3. Set your password

```bash
echo "AWSEVENTS_PASSWORD=your_password_here" > .env
```

`.env` is gitignored — the password never touches the repo.

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

---

## How the scheduler works

Given scored sessions, the greedy optimizer:

1. Groups sessions by day
2. Picks the highest-scoring session that fits in the remaining day
3. Applies a **venue-hop penalty** (`location_change_penalty` in config) when the next pick requires a different cluster — biases toward staying in the same building
4. Respects `max_sessions_per_day` and `buffer_minutes` between sessions

Venue clusters and travel-time estimates are defined in `config.yaml` and can be updated for future conference years.

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

All views use the **Glass** design: frosted cards, gradient background, Geist typography, venue cluster colors (indigo / pink / teal / amber), light + dark mode.

---

## Project structure

```
agent/
  catalog.py      — public AWS Events API fallback fetcher
  wishlist.py     — catalog.awsevents.com REST + Playwright scraper
  scorer.py       — keyword × AOI × level scoring
  scheduler.py    — greedy daily optimizer with venue-hop penalty
  registrar.py    — Playwright auto-registration
  wizard.py       — 7-step interactive setup wizard
  html_views.py   — self-contained HTML view generator (Glass design)
  main.py         — CLI entry point (click)
config.yaml       — all user preferences, no credentials
.env              — AWSEVENTS_PASSWORD only (gitignored)
```

---

## Security notes

- **Password** — stored only in `.env` (gitignored). Never committed.
- **Email** — stored in `config.yaml`. Replace with your own before use.
- **Catalog API IDs** — Rainfocus widget IDs embedded in the public catalog page. Configurable in `config.yaml` under `catalog_api`.
- **Cookies** — read locally from Chrome Profile 1 via `browser_cookie3`. Not stored, not transmitted anywhere except to `catalog.awsevents.com`.

---

## License

MIT
