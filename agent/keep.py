"""User-approved enrolled sessions ("keep") and the reservable sessions they rule out.

A kept session is never released. When the site reports a schedule conflict for a session on a day
that has a kept session, that session is declined: later openings for it are skipped silently.
"""
from __future__ import annotations

import json
from pathlib import Path

_PATH = Path("kept_sessions.json")


def load(path: Path = _PATH) -> dict:
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    return {"kept": state.get("kept", {}), "declined": state.get("declined", {})}


def _save(state: dict, path: Path) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def keep(code: str, over: list[str] | None = None, path: Path = _PATH) -> dict:
    """Approve an enrolled session; optionally decline the sessions it was chosen over."""
    state = load(path)
    state["kept"][code] = {"declined": sorted(set(state["kept"].get(code, {}).get("declined", [])) | set(over or []))}
    for other in over or []:
        state["declined"][other] = code
    _save(state, path)
    return state


def unkeep(code: str, path: Path = _PATH) -> dict:
    """Withdraw an approval and re-open every session that was declined because of it."""
    state = load(path)
    state["kept"].pop(code, None)
    state["declined"] = {c: by for c, by in state["declined"].items() if by != code}
    _save(state, path)
    return state


def decline_conflicts(conflicted: dict[str, str], sessions: dict[str, dict], path: Path = _PATH) -> list[tuple[str, str]]:
    """Decline conflicted sessions ({event_id: code}) that fall on a day with a kept, enrolled session.

    Returns [(declined_code, kept_code)] for the newly declined ones. Conflicts on days with no kept
    session are left alone: they still need a human decision.
    """
    state = load(path)
    kept_by_day = {
        s["scheduled_start"][:10]: s["code"]
        for s in sessions.values()
        if s["status"] == "enrolled" and s["code"] in state["kept"]
    }
    newly: list[tuple[str, str]] = []
    for eid, code in conflicted.items():
        by = kept_by_day.get(sessions[eid]["scheduled_start"][:10])
        if by and code not in state["declined"]:
            state["declined"][code] = by
            newly.append((code, by))
    if newly:
        _save(state, path)
    return newly


def is_declined(code: str | None, path: Path = _PATH) -> bool:
    return bool(code) and code in load(path)["declined"]
