"""Reserve a seat through the catalog API - the same call the page's "Reserve a seat" button makes.

POST /api/rainfocus/v2/addSession (form: sessionId, sessionTimeId, waitlist, wasRecommended, virtual),
taken from the catalog widget bundle. The page checks schedule conflicts client-side, so we do the same
here before calling: an existing reservation is never double-booked or released. Every booking is
confirmed against /api/myData; anything unconfirmed is reported as "error" so the caller can fall back
to the browser.
"""

from __future__ import annotations

import httpx

from agent.seats import _post

ADD_SESSION_PATH = "/api/rainfocus/v2/addSession"


def _enrolled_ids(my: dict) -> set[str]:
    return {m.get("sessionID") for m in my.get("mySchedule", [])} | {
        t.get("sessionID") for m in my.get("mySchedule", []) for t in m.get("times", [])
    }


def find_conflict(my_schedule: list[dict], slot: list[str]) -> str | None:
    """Title of an enrolled session overlapping `slot` ([start, end] as 'YYYY-MM-DD HH:MM'), else None."""
    start, end = slot
    for m in my_schedule:
        if m.get("allowDoubleBooking"):
            continue
        m_start = f"{m.get('date')} {m.get('startTime')}"
        m_end = f"{m.get('date')} {m.get('endTime')}"
        if start < m_end and m_start < end:  # ISO-like strings compare chronologically
            return str(m.get("title") or m.get("code") or "an existing session")
    return None


async def reserve(client: httpx.AsyncClient, session_id: str, time_id: str, slot: list[str]) -> tuple[str, str]:
    """Returns (outcome, message); outcome in ok | conflict | error (same contract as the browser path)."""
    try:
        my = await _post(client, "/api/myData")
        if "mySchedule" not in my:
            return "error", "not authenticated"
        if session_id in _enrolled_ids(my):
            return "ok", "already enrolled"
        clash = find_conflict(my["mySchedule"], slot)
        if clash:
            return "conflict", f"conflicts with an existing reservation ({clash[:50]}); left untouched"
        resp = await _post(client, ADD_SESSION_PATH, {
            "sessionId": session_id, "sessionTimeId": time_id,
            "waitlist": "false", "wasRecommended": "false", "virtual": "false",
        })
        if session_id in _enrolled_ids(await _post(client, "/api/myData")):
            return "ok", "reserved via API"
        return "error", f"addSession not confirmed: {str(resp.get('responseMessage') or resp.get('responseCode') or '')[:80]}"
    except (httpx.HTTPError, ValueError) as exc:
        return "error", f"API reserve failed: {type(exc).__name__}"
