"""MCP server exposing the re:Invent planner as LLM-callable tools.

Any LLM that speaks MCP can use this — Bedrock is not required.
Keyword scoring is the default; Bedrock re-ranking is opt-in via config.yaml
(llm.enabled: true) and falls back silently if unavailable.

Entrypoint: `awsevents-mcp`  (stdio transport, compatible with Claude Desktop,
Cursor, Continue, and any other MCP host)

Tools
-----
list_sessions   — fetch and score sessions; returns top-N as JSON
plan_schedule   — build an optimised daily schedule; returns schedule JSON
get_schedule    — return the saved schedule.json (if it exists)
get_config      — return the active preferences from config.yaml
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

from mcp.server.fastmcp import FastMCP

load_dotenv()

# Config path: env override or default to config.yaml in CWD
_CONFIG_PATH = Path(os.getenv("AWSEVENTS_CONFIG", "config.yaml"))
_SCHEDULE_PATH = Path("schedule.json")

mcp = FastMCP(
    name="awsevents-planner",
    instructions=(
        "Tools for planning an AWS re:Invent schedule. "
        "Use list_sessions to explore sessions, plan_schedule to build an "
        "optimised schedule, get_schedule to read the saved schedule, and "
        "get_config to inspect the current preferences."
    ),
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {_CONFIG_PATH}. "
            "Set AWSEVENTS_CONFIG env var or run from the project directory."
        )
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _make_scorer(config: dict):
    prefs = config.get("preferences", {})
    llm_cfg = config.get("llm", {})
    if llm_cfg.get("enabled", False):
        try:
            import boto3  # noqa: F401
            from agent.llm_scorer import LLMScorer
            return LLMScorer(llm_cfg, prefs)
        except (ImportError, Exception):
            pass  # silent fallback to keyword scorer
    from agent.scorer import Scorer
    return Scorer(prefs)


def _session_to_dict(s: Any, scheduled_start: Optional[str] = None) -> dict:
    return {
        "event_id": s.event_id,
        "title": s.title,
        "description": (s.description or "")[:300],
        "start_date": s.start_date.isoformat() if s.start_date else None,
        "start_time": s.start_time,
        "location": s.location,
        "location_mode": s.location_mode,
        "learning_level": s.learning_level,
        "event_type": s.event_type,
        "score": round(s.score, 4),
        "registration_url": s.registration_url,
        **({"scheduled_start": scheduled_start} if scheduled_start else {}),
    }


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_sessions(
    min_score: float = 0.1,
    keyword: str = "",
    day: str = "",
    top: int = 30,
) -> str:
    """Fetch and score re:Invent sessions from the AWS Events catalog.

    Args:
        min_score: Minimum relevance score 0–1 (default 0.1).
        keyword:   Optional substring to filter session titles/descriptions.
        day:       Optional day filter as YYYY-MM-DD (e.g. "2026-12-02").
        top:       Maximum number of sessions to return (default 30).

    Returns JSON array of sessions sorted by score descending.
    """
    from agent.catalog import fetch_sessions
    from agent.wishlist import load_catalog

    config = _load_config()
    ev_cfg = config.get("event", {})
    scorer = _make_scorer(config)

    reinvent_path = Path("reinvent_catalog.json")
    if reinvent_path.exists():
        sessions = load_catalog()
        scored = scorer.score_reinvent_sessions(sessions)
    else:
        start = date.fromisoformat(ev_cfg["start_date"]) if ev_cfg.get("start_date") else None
        end = date.fromisoformat(ev_cfg["end_date"]) if ev_cfg.get("end_date") else None
        sessions = await fetch_sessions(
            start_date=start,
            end_date=end,
            location_text=ev_cfg.get("location_keyword"),
            location_mode=ev_cfg.get("location_mode"),
        )
        scored = scorer.score_all(sessions)

    results = [s for s in scored if s.score >= min_score]

    if day:
        results = [s for s in results if s.start_date and s.start_date.isoformat() == day]

    if keyword:
        kw = keyword.lower()
        results = [s for s in results if kw in s.title.lower() or kw in (s.description or "").lower()]

    results.sort(key=lambda s: s.score, reverse=True)
    results = results[:top]

    return json.dumps([_session_to_dict(s) for s in results], indent=2)


@mcp.tool()
async def plan_schedule(day: str = "") -> str:
    """Build an optimised daily schedule from scored sessions.

    Scores all sessions, applies venue-hop penalties, and picks the best
    non-overlapping set per day. Does NOT overwrite schedule.json.

    Args:
        day: Optional day filter YYYY-MM-DD. Omit for all conference days.

    Returns JSON array of scheduled sessions with start/end times.
    """
    from agent.catalog import fetch_sessions
    from agent.scheduler import build_schedule
    from agent.wishlist import load_catalog

    config = _load_config()
    ev_cfg = config.get("event", {})
    scorer = _make_scorer(config)

    reinvent_path = Path("reinvent_catalog.json")
    if reinvent_path.exists():
        sessions = load_catalog()
        scored = scorer.score_reinvent_sessions(sessions)
    else:
        start = date.fromisoformat(ev_cfg["start_date"]) if ev_cfg.get("start_date") else None
        end = date.fromisoformat(ev_cfg["end_date"]) if ev_cfg.get("end_date") else None
        sessions = await fetch_sessions(
            start_date=start,
            end_date=end,
            location_text=ev_cfg.get("location_keyword"),
            location_mode=ev_cfg.get("location_mode"),
        )
        scored = scorer.score_all(sessions)

    above = [s for s in scored if s.score >= 0.1]
    schedule = build_schedule(above, config)

    if day:
        schedule = [ss for ss in schedule if ss.day == day]

    result = []
    for ss in schedule:
        d = _session_to_dict(ss.session, scheduled_start=ss.start.isoformat())
        d["scheduled_end"] = ss.end.isoformat()
        d["day"] = ss.day
        if ss.backup:
            d["backup"] = True
            d["backup_note"] = ss.backup_note
        result.append(d)

    return json.dumps(result, indent=2)


@mcp.tool()
def get_schedule() -> str:
    """Return the saved schedule from schedule.json.

    Returns the current planned schedule as a JSON array, or an empty array
    if no schedule has been saved yet. Each session includes primary/backup flags.
    """
    if not _SCHEDULE_PATH.exists():
        return json.dumps([])
    with open(_SCHEDULE_PATH) as f:
        return f.read()


@mcp.tool()
def get_config() -> str:
    """Return the active planner preferences from config.yaml.

    Returns the preferences section (domains, levels, session types, etc.)
    plus the event date range. Credentials are excluded.
    """
    config = _load_config()
    safe = {
        "event": config.get("event", {}),
        "preferences": config.get("preferences", {}),
        "venue_clusters": {k: v.get("label") for k, v in config.get("venue_clusters", {}).items()},
        "llm": {
            "enabled": config.get("llm", {}).get("enabled", False),
            "model_id": config.get("llm", {}).get("model_id", ""),
            "region": config.get("llm", {}).get("region", ""),
        },
    }
    return json.dumps(safe, indent=2)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
