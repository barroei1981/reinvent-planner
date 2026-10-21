"""Seat availability + enrollment status straight from the catalog API (no browser).

The registration pages render the whole event catalog for any ?session= URL, so
scraping them cannot tell one session from another. The catalog API exposes the
real numbers per session (seatsRemaining / waitlistRemaining) and /api/myData
lists what the logged-in user is actually enrolled in.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx

from agent.scheduler import ScheduledSession
from agent.wishlist import _CATALOG_API_BASE, _load_chrome_profile1_cookies, _rf_headers

STATUS_PATH = Path("/tmp/awsevents_seats.json")
ALERT_LOG = Path("/tmp/awsevents_alerts.log")
_ALERT_STATUSES = {"reservable", "waitlist_open"}
_PAGE_SIZE = 50
_MAX_ATTEMPTS = 3


async def _post(client: httpx.AsyncClient, path: str, body: dict | None = None) -> dict:
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            r = await client.post(f"{_CATALOG_API_BASE}{path}", content=urlencode(body or {}))
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            if attempt == _MAX_ATTEMPTS:
                raise
            await asyncio.sleep(0.5 * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


def _items(d: dict) -> list[dict]:
    return d.get("items") or (d.get("sectionList") or [{}])[0].get("items", [])


def _is_walk_up(item: dict, time_entry: dict) -> bool:
    """Walk-up sessions have no schedule access (can't be pre-reserved) and carry a 'Walk up only' attribute."""
    flagged = any(
        "walk up only" in str(a.get("attribute", "")).lower() for a in item.get("attributevalues", [])
    )
    return flagged or not time_entry.get("scheduleAccess")


def classify(item: dict | None, time_entry: dict | None, enrolled: bool) -> str:
    if enrolled:
        return "enrolled"
    if item is None or time_entry is None:
        return "unknown"
    if _is_walk_up(item, time_entry):
        return "walk_up"
    if int(time_entry.get("seatsRemaining") or 0) > 0:
        return "reservable"
    if int(time_entry.get("waitlistRemaining") or 0) > 0:
        return "waitlist_open"
    return "full"


async def fetch_seat_status(
    schedule: list[ScheduledSession], rf_profile_id: str, rf_widget_id: str
) -> dict:
    wanted = {ss.session.event_id: ss for ss in schedule}
    cookies, rfjwt = _load_chrome_profile1_cookies()
    headers = _rf_headers(rf_profile_id, rf_widget_id)
    headers.update({
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://registration.awsevents.com",
        "Referer": "https://registration.awsevents.com/flow/awsevents/reinvent2026/event-catalog/page/eventCatalog",
    })
    if rfjwt:
        headers["rfAuthToken"] = rfjwt

    found: dict[str, dict] = {}
    async with httpx.AsyncClient(cookies=cookies, headers=headers, timeout=30) as client:
        my = await _post(client, "/api/myData")
        if "mySchedule" not in my:
            raise RuntimeError("not authenticated: /api/myData has no mySchedule (log in to Chrome Profile 1)")
        enrolled_ids = {
            t.get("sessionID") for m in my["mySchedule"] for t in m.get("times", [])
        } | {m.get("sessionID") for m in my["mySchedule"]}

        offset = 0
        while offset < 5000 and len(found) < len(wanted):
            items = _items(await _post(client, "/api/sessions", {"from": offset, "size": _PAGE_SIZE}))
            if not items:
                break
            for it in items:
                if it.get("sessionID") in wanted:
                    found[it["sessionID"]] = it
            offset += _PAGE_SIZE

    sessions = {}
    summary: dict[str, int] = {}
    for sid, ss in wanted.items():
        times = (found.get(sid) or {}).get("times", [])
        slot = ss.start.strftime("%Y-%m-%d %H:%M") if ss.start else ""
        entry = next((t for t in times if f"{t.get('date')} {t.get('startTime')}" == slot), None) or (
            times[0] if times else None
        )
        status = classify(found.get(sid), entry, bool(entry and entry.get("sessionID") in enrolled_ids) or sid in enrolled_ids)
        summary[status] = summary.get(status, 0) + (0 if ss.backup else 1)
        sessions[sid] = {
            "title": ss.session.title,
            "code": (found.get(sid) or {}).get("code"),
            "status": status,
            "scheduled_start": ss.start.isoformat() if ss.start else "",
            "level": ss.session.learning_level or "Unknown",
            "backup": ss.backup,
            "seats_remaining": entry.get("seatsRemaining") if entry else None,
            "capacity": entry.get("capacity") if entry else None,
            "waitlist_remaining": entry.get("waitlistRemaining") if entry else None,
        }
    return {
        "last_check": datetime.now().astimezone().isoformat(),
        "sessions": sessions,
        "summary": summary,
        "enrolled_total": len(my["mySchedule"]),
    }


def write_status(data: dict) -> None:
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STATUS_PATH)


def load_previous() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def find_openings(prev: dict, new: dict) -> list[dict]:
    """Sessions that just became reservable/waitlistable (were not before). No baseline -> no alerts."""
    old = prev.get("sessions")
    if not old:
        return []
    return [
        s for sid, s in new["sessions"].items()
        if s["status"] in _ALERT_STATUSES and old.get(sid, {}).get("status") not in _ALERT_STATUSES
    ]


def notify(openings: list[dict]) -> None:
    lines = [
        f"{s['code'] or ''} {s['scheduled_start'][5:16].replace('T', ' ')} {s['title'][:60]}"
        f" ({s['status']}, {s['seats_remaining']} seats)"
        for s in openings
    ]
    with ALERT_LOG.open("a") as f:
        f.write(f"{datetime.now().astimezone().isoformat()}\n" + "\n".join(f"  {ln}" for ln in lines) + "\n")
    notify_text(f"{len(openings)} session(s) opened: " + "; ".join(ln[:70] for ln in lines[:3]))


def notify_text(text: str, title: str = "re:Invent seats") -> None:
    script = f'display notification {json.dumps(text)} with title {json.dumps(title)} sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass  # notification is best-effort; the alert log / status events are the record


def add_events(data: dict, prev: dict, texts: list[str]) -> None:
    """Carry the last 20 activity events forward from the previous snapshot and append new ones."""
    now = datetime.now().astimezone().isoformat()
    data["events"] = (prev.get("events") or [])[-20 + len(texts):] + [{"time": now, "text": t} for t in texts]
