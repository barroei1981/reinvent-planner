"""MCP server exposing the re:Invent planner as LLM-callable tools.

Two transports:

  stdio           Local use — Claude Desktop, Cursor, Continue, VS Code, etc.
                  `uv run awsevents-mcp`

  http            Remote use — Claude.ai, ChatGPT, Gemini, Perplexity, any
                  HTTP-capable MCP host.
                  `uv run awsevents-mcp --transport http --host 0.0.0.0 --port 8000`

No LLM is required to use this server.  Bedrock re-ranking is opt-in via
config.yaml (llm.enabled: true) and falls back silently if unavailable.

Auth (HTTP mode only)
---------------------
Set AWSEVENTS_API_KEY in the environment.  The server will reject requests
that do not include:
    Authorization: Bearer <your-key>

Leave AWSEVENTS_API_KEY unset to run without auth (local / trusted-network use).

Tools
-----
list_sessions   — fetch and score sessions; returns top-N as JSON
plan_schedule   — build an optimised daily schedule; returns schedule JSON
get_schedule    — return the saved schedule.json (if it exists)
get_config      — return the active preferences from config.yaml
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Optional

import click
import yaml
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

_CONFIG_PATH = Path(os.getenv("AWSEVENTS_CONFIG", "config.yaml"))
_SCHEDULE_PATH = Path("schedule.json")

mcp = FastMCP(
    name="awsevents-planner",
    instructions=(
        "Tools for planning an AWS re:Invent 2026 schedule. "
        "Use list_sessions to explore sessions by score/keyword/day, "
        "plan_schedule to build an optimised daily schedule, "
        "get_schedule to read the saved schedule, and "
        "get_config to inspect the current preferences."
    ),
    stateless_http=True,   # each HTTP request is independent — needed for multi-client hosts
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
        except Exception:
            pass
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


async def _fetch_and_score(config: dict):
    """Fetch sessions from the best available source and apply scoring."""
    from agent.catalog import fetch_sessions

    ev_cfg = config.get("event", {})
    scorer = _make_scorer(config)
    reinvent_path = Path("reinvent_catalog.json")

    if reinvent_path.exists():
        from agent.wishlist import load_catalog
        sessions = load_catalog()
        return scorer.score_reinvent_sessions(sessions)

    start = date.fromisoformat(ev_cfg["start_date"]) if ev_cfg.get("start_date") else None
    end = date.fromisoformat(ev_cfg["end_date"]) if ev_cfg.get("end_date") else None
    sessions = await fetch_sessions(
        start_date=start, end_date=end,
        location_text=ev_cfg.get("location_keyword"),
        location_mode=ev_cfg.get("location_mode"),
    )
    return scorer.score_all(sessions)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_sessions(
    min_score: float = 0.1,
    keyword: str = "",
    day: str = "",
    top: int = 30,
) -> str:
    """Fetch and score re:Invent 2026 sessions from the AWS Events catalog.

    Args:
        min_score: Minimum relevance score 0–1 (default 0.1).
        keyword:   Optional substring to filter session titles/descriptions.
        day:       Optional day filter as YYYY-MM-DD (e.g. "2026-12-02").
        top:       Maximum number of sessions to return (default 30).

    Returns JSON array of sessions sorted by score descending.
    Each session includes event_id, title, description (truncated), start_date,
    start_time, location, learning_level, event_type, score, registration_url.
    """
    config = _load_config()
    scored = await _fetch_and_score(config)

    results = [s for s in scored if s.score >= min_score]
    if day:
        results = [s for s in results if s.start_date and s.start_date.isoformat() == day]
    if keyword:
        kw = keyword.lower()
        results = [s for s in results if kw in s.title.lower() or kw in (s.description or "").lower()]

    results.sort(key=lambda s: s.score, reverse=True)
    return json.dumps([_session_to_dict(s) for s in results[:top]], indent=2)


@mcp.tool()
async def plan_schedule(day: str = "") -> str:
    """Build an optimised daily re:Invent 2026 schedule.

    Scores all sessions, applies venue-hop penalties to minimise travel between
    Las Vegas convention centres, and selects the best non-overlapping sessions
    per day. Does NOT overwrite the local schedule.json.

    Args:
        day: Optional day filter YYYY-MM-DD. Omit for all conference days.

    Returns JSON array of scheduled sessions with start/end times, location,
    score, and backup flags.
    """
    from agent.scheduler import build_schedule

    config = _load_config()
    scored = await _fetch_and_score(config)
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
    if no schedule has been saved yet. Includes primary and backup sessions.
    """
    if not _SCHEDULE_PATH.exists():
        return json.dumps([])
    with open(_SCHEDULE_PATH) as f:
        return f.read()


@mcp.tool()
def get_config() -> str:
    """Return the active planner preferences from config.yaml.

    Returns event date range, domain keywords, level preferences, session types,
    and venue clusters. Credentials and passwords are excluded.
    """
    config = _load_config()
    return json.dumps({
        "event": config.get("event", {}),
        "preferences": config.get("preferences", {}),
        "venue_clusters": {k: v.get("label") for k, v in config.get("venue_clusters", {}).items()},
        "llm": {
            "enabled": config.get("llm", {}).get("enabled", False),
            "model_id": config.get("llm", {}).get("model_id", ""),
            "region": config.get("llm", {}).get("region", ""),
        },
    }, indent=2)


# ── CLI entry point ───────────────────────────────────────────────────────────

@click.command()
@click.option(
    "--transport",
    default="stdio",
    type=click.Choice(["stdio", "http"]),
    show_default=True,
    help="stdio = local MCP client; http = remote (Claude.ai / ChatGPT / Gemini / Perplexity).",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host (HTTP mode only).")
@click.option("--port", default=8000, show_default=True, type=int, help="Listen port (HTTP mode only).")
def main(transport: str, host: str, port: int) -> None:
    """Start the re:Invent planner MCP server.

    \b
    Local use (Claude Desktop, Cursor, VS Code):
        awsevents-mcp

    \b
    Remote use (Claude.ai, ChatGPT, Gemini, Perplexity):
        awsevents-mcp --transport http --host 0.0.0.0 --port 8000

    Set AWSEVENTS_API_KEY in the environment to require a Bearer token.
    """
    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    # ── HTTP mode ─────────────────────────────────────────────────────────────
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    api_key = os.getenv("AWSEVENTS_API_KEY", "")

    app = mcp.streamable_http_app()

    if api_key:
        class _BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                # Pass health/info requests unauthenticated for MCP discovery
                if request.url.path in ("/", "/health"):
                    return await call_next(request)
                auth = request.headers.get("Authorization", "")
                if not (auth.startswith("Bearer ") and auth[7:] == api_key):
                    return Response(
                        content=json.dumps({"error": "Unauthorized — set Authorization: Bearer <AWSEVENTS_API_KEY>"}),
                        status_code=401,
                        media_type="application/json",
                    )
                return await call_next(request)

        app.add_middleware(_BearerAuth)
        click.echo(f"Auth: Bearer token required (AWSEVENTS_API_KEY is set)")
    else:
        click.echo("Auth: none — set AWSEVENTS_API_KEY to require a Bearer token")

    mcp_url = f"http://{host}:{port}/mcp"
    click.echo(f"MCP endpoint: {mcp_url}")
    click.echo(f"Clients: Claude.ai → Settings → Integrations → Add MCP Server → {mcp_url}")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
