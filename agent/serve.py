"""Interactive local web server for schedule editing.

Serves the schedule HTML view with live edit capability:
  - edit buttons on every session card
  - inline alternatives panel (fetched from scored catalog)
  - swap-in / add-as-backup actions write back to schedule.json immediately

Routes
------
GET  /                     interactive schedule HTML
GET  /api/schedule         current schedule.json as JSON
GET  /api/alternatives     scored alternatives for a slot
POST /api/swap             replace or add-as-backup
POST /api/remove           remove a session
"""

from __future__ import annotations

import json
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

load_dotenv()

_SCHEDULE_PATH = Path("schedule.json")
_CATALOG_PATH = Path("reinvent_catalog.json")
_CONFIG_PATH = Path("config.yaml")

_CONF_DAYS = {
    "2026-11-30", "2026-12-01", "2026-12-02",
    "2026-12-03", "2026-12-04", "2026-12-05",
}


class _State:
    config: dict = {}
    scored: list = []   # scored Session objects, pre-loaded on startup
    port: int = 8080


_st = _State()


def _load_schedule() -> list[dict]:
    if _SCHEDULE_PATH.exists():
        with open(_SCHEDULE_PATH) as f:
            return json.load(f)
    return []


def _save_schedule(schedule: list[dict]) -> None:
    with open(_SCHEDULE_PATH, "w") as f:
        json.dump(schedule, f, indent=2)


@asynccontextmanager
async def _lifespan(app):
    with open(_CONFIG_PATH) as f:
        _st.config = yaml.safe_load(f)

    prefs = _st.config.get("preferences", {})
    from agent.scorer import Scorer
    scorer = Scorer(prefs)

    if _CATALOG_PATH.exists():
        from agent.wishlist import load_catalog
        sessions = load_catalog()
        _st.scored = scorer.score_reinvent_sessions(sessions)
    else:
        from agent.catalog import fetch_sessions
        ev_cfg = _st.config.get("event", {})
        start = date.fromisoformat(ev_cfg["start_date"]) if ev_cfg.get("start_date") else None
        end = date.fromisoformat(ev_cfg["end_date"]) if ev_cfg.get("end_date") else None
        import asyncio
        sessions = await fetch_sessions(
            start_date=start, end_date=end,
            location_text=ev_cfg.get("location_keyword"),
            location_mode=ev_cfg.get("location_mode"),
        )
        _st.scored = scorer.score_all(sessions)

    yield


# ── Route handlers ─────────────────────────────────────────────────────────────

async def get_index(request: Request) -> HTMLResponse:
    from agent.html_views import html_schedule
    schedule = _load_schedule()
    html = html_schedule(schedule, interactive_port=_st.port)
    return HTMLResponse(html)


async def get_schedule_api(request: Request) -> JSONResponse:
    return JSONResponse(_load_schedule())


async def get_alternatives(request: Request) -> JSONResponse:
    slot = request.query_params.get("slot", "")   # YYYY-MM-DDTHH:MM
    exclude_id = request.query_params.get("exclude", "")

    if len(slot) < 16:
        return JSONResponse({"error": "slot required (YYYY-MM-DDTHH:MM)"}, status_code=400)

    day = slot[:10]
    schedule = _load_schedule()

    # All scheduled IDs except the one being replaced
    scheduled_ids = {s["event_id"] for s in schedule if s["event_id"] != exclude_id}
    # Other sessions on the same day (for conflict check)
    other_day = [s for s in schedule if s.get("start_date") == day and s["event_id"] != exclude_id]

    def _range(s: dict) -> Optional[tuple[datetime, datetime]]:
        try:
            return datetime.fromisoformat(s["scheduled_start"]), datetime.fromisoformat(s["scheduled_end"])
        except Exception:
            return None

    other_ranges = [r for s in other_day if (r := _range(s))]

    def _conflicts(cand) -> bool:
        if not cand.start_time or str(cand.start_date) != day:
            return False
        try:
            cs = datetime.strptime(f"{day}T{cand.start_time}", "%Y-%m-%dT%H:%M")
        except Exception:
            return False
        dur = 120 if "workshop" in (cand.event_type or "").lower() else 60
        ce = cs + timedelta(minutes=dur)
        return any(max(cs, os) < min(ce, oe) for os, oe in other_ranges)

    alts = [
        s for s in _st.scored
        if s.event_id not in scheduled_ids
        and str(s.start_date) == day
        and str(s.start_date) in _CONF_DAYS
        and s.score >= 0.15
        and not _conflicts(s)
    ]
    alts.sort(key=lambda s: s.score, reverse=True)

    return JSONResponse([{
        "event_id": s.event_id,
        "title": s.title,
        "description": (s.description or "")[:200],
        "start_time": s.start_time,
        "location": (s.location or "").split("|")[0].strip()[:30],
        "learning_level": s.learning_level,
        "event_type": s.event_type,
        "score": round(s.score, 3),
        "registration_url": s.registration_url,
    } for s in alts[:15]])


async def post_swap(request: Request) -> JSONResponse:
    """Replace or add-as-backup.

    Body: {slot: "YYYY-MM-DDTHH:MM", new_event_id: "...", mode: "replace"|"backup"}
    """
    body = await request.json()
    slot = body.get("slot", "")
    new_event_id = body.get("new_event_id", "")
    mode = body.get("mode", "replace")

    new_sess = next((s for s in _st.scored if s.event_id == new_event_id), None)
    if not new_sess:
        return JSONResponse({"error": "session not found in catalog"}, status_code=404)

    day = slot[:10]
    schedule = _load_schedule()

    # Current primary at this slot
    target = next(
        (s for s in schedule if (s.get("scheduled_start") or "")[:16] == slot and not s.get("backup")),
        None
    )

    try:
        new_start = datetime.strptime(f"{day}T{new_sess.start_time}", "%Y-%m-%dT%H:%M")
    except Exception:
        return JSONResponse({"error": "cannot parse session start time"}, status_code=400)

    dur_min = 120 if "workshop" in (new_sess.event_type or "").lower() else 60
    new_end = new_start + timedelta(minutes=dur_min)

    # Build the schedule entry — backup shares the primary's slot key
    entry: dict[str, Any] = {
        "event_id": new_sess.event_id,
        "title": new_sess.title,
        "description": (new_sess.description or "")[:300],
        "start_date": day,
        "start_time": new_sess.start_time,
        "location": new_sess.location,
        "location_mode": new_sess.location_mode,
        "learning_level": new_sess.learning_level,
        "event_type": new_sess.event_type,
        "score": round(new_sess.score, 4),
        "registration_url": new_sess.registration_url,
        "scheduled_start": new_start.isoformat(),
        "scheduled_end": new_end.isoformat(),
    }

    if mode == "backup":
        entry["backup"] = True
        entry["backup_note"] = (
            f"Alternative for: {target['title'][:55]}" if target else "Backup session"
        )
        # Slot key must match the primary so registrar can pair them
        entry["scheduled_start"] = f"{slot}:00" if len(slot) == 16 else slot
        schedule.append(entry)
    else:
        if target:
            schedule = [entry if s["event_id"] == target["event_id"] else s for s in schedule]
        else:
            schedule.append(entry)

    schedule.sort(key=lambda s: (s.get("start_date", ""), s.get("scheduled_start") or ""))
    _save_schedule(schedule)
    return JSONResponse({"ok": True})


async def get_event_detail(request: Request) -> JSONResponse:
    """Full record for one session — complete description, all fields."""
    event_id = request.path_params["event_id"]
    session = next((s for s in _st.scored if s.event_id == event_id), None)
    if not session:
        return JSONResponse({"error": f"event {event_id!r} not found"}, status_code=404)
    return JSONResponse({
        "event_id": session.event_id,
        "title": session.title,
        "description": session.description or "",
        "start_date": str(session.start_date) if session.start_date else None,
        "start_time": session.start_time,
        "time_zone": session.time_zone,
        "location": session.location,
        "location_mode": session.location_mode,
        "learning_level": session.learning_level,
        "event_type": session.event_type,
        "partner_name": session.partner_name,
        "score": round(session.score, 4),
        "registration_url": session.registration_url,
        "learn_more_url": session.learn_more_url,
    })


async def post_remove(request: Request) -> JSONResponse:
    body = await request.json()
    event_id = body.get("event_id", "")
    schedule = [s for s in _load_schedule() if s["event_id"] != event_id]
    _save_schedule(schedule)
    return JSONResponse({"ok": True})


# ── App factory ────────────────────────────────────────────────────────────────

def create_app(port: int = 8080) -> Starlette:
    _st.port = port
    return Starlette(
        lifespan=_lifespan,
        routes=[
            Route("/", get_index),
            Route("/api/schedule", get_schedule_api),
            Route("/api/alternatives", get_alternatives),
            Route("/api/event/{event_id}", get_event_detail),
            Route("/api/swap", post_swap, methods=["POST"]),
            Route("/api/remove", post_remove, methods=["POST"]),
        ],
    )
