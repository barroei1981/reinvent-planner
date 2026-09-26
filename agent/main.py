"""CLI entry point for the re:Invent session planner agent.

Commands:
  sync-wishlist  — log in to registration.awsevents.com, scrape full catalog + wishlist
  plan           — score + schedule (uses scraped catalog when available, public API otherwise)
  list           — show all matching sessions with scores (no scheduling)
  watch          — load saved schedule and auto-register when seats open
  register       — register from saved schedule immediately (no polling)
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date
from pathlib import Path
from typing import Optional

import click
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box

from agent.catalog import fetch_sessions, Session
from agent.scorer import Scorer
from agent.scheduler import build_schedule, ScheduledSession


def _make_scorer(config: dict):
    """Return LLMScorer when llm.enabled=true and boto3 is available, else Scorer."""
    prefs = config.get("preferences", {})
    llm_cfg = config.get("llm", {})
    if llm_cfg.get("enabled", False):
        try:
            import boto3  # noqa: F401
            from agent.llm_scorer import LLMScorer
            scorer = LLMScorer(llm_cfg, prefs)
            console.print("[dim]LLM scoring enabled (Bedrock)[/dim]")
            return scorer
        except ImportError:
            console.print("[yellow]llm.enabled=true but boto3 is not installed — using keyword scorer[/yellow]")
    return Scorer(prefs)

load_dotenv()
console = Console(width=160)

_CONFIG_PATH = Path("config.yaml")
_SCHEDULE_PATH = Path("schedule.json")


_DAY_NAMES_PLAN = {
    "2026-11-30": "Mon Nov 30", "2026-12-01": "Tue Dec 1",
    "2026-12-02": "Wed Dec 2",  "2026-12-03": "Thu Dec 3",
    "2026-12-04": "Fri Dec 4",  "2026-12-05": "Sat Dec 5",
}


def _run_approval_gate(existing: list[dict], new_sessions: list[dict]) -> Optional[list[dict]]:
    """Per-slot approval gate comparing existing schedule to a new plan.

    For each changed slot the user chooses:
      1 — keep current primary + add Bedrock pick as backup
      2 — switch to Bedrock primary + keep current as backup
      3 — keep only current  (ignore Bedrock)
      4 — take only Bedrock  (drop current)

    New slots (Bedrock added) and dropped slots (Bedrock removed) get a
    simple yes/no prompt.

    Returns the final sessions list to write, or None if the user cancels.
    Existing backup sessions are always preserved unless explicitly dropped.
    """
    existing_backups = [s for s in existing if s.get("backup")]
    existing_primaries = {
        s["scheduled_start"][:16]: s
        for s in existing
        if not s.get("backup") and s.get("scheduled_start")
    }
    new_by_slot = {
        s["scheduled_start"][:16]: s
        for s in new_sessions
        if s.get("scheduled_start")
    }

    all_slots = sorted(set(existing_primaries) | set(new_by_slot))
    unchanged, replacements, additions, drops = [], [], [], []

    for slot in all_slots:
        e = existing_primaries.get(slot)
        n = new_by_slot.get(slot)
        if e and n:
            if e["event_id"] == n["event_id"]:
                unchanged.append((slot, n))   # take new version (score may differ)
            else:
                replacements.append((slot, e, n))
        elif n:
            additions.append((slot, n))
        else:
            drops.append((slot, e))

    if not replacements and not additions and not drops:
        console.print("\n[dim]No session changes vs existing schedule.[/dim]")
        # Re-save with updated scores; preserve existing backups
        final = [s for _, s in unchanged]
        for b in existing_backups:
            if b["event_id"] not in {s["event_id"] for s in final}:
                final.append(b)
        final.sort(key=lambda s: (s.get("start_date", ""), s.get("scheduled_start", "")))
        return final

    final: list[dict] = [s for _, s in unchanged]

    def _venue(s: dict) -> str:
        return (s.get("location") or "—").split("|")[0].strip()[:22]

    # ── Replacements: per-slot decision ───────────────────────────────────────
    if replacements:
        console.print(f"\n[bold yellow]⚡  {len(replacements)} slot(s) — Bedrock recommends a different session[/bold yellow]")

    for slot, curr, new in replacements:
        day, time = slot[:10], slot[11:16]
        console.print(
            f"\n  [bold]{_DAY_NAMES_PLAN.get(day, day)}  {time}[/bold]"
        )
        console.print(
            f"  [dim]Current :[/dim] [cyan]{curr['title'][:65]}[/cyan]\n"
            f"           [dim]score {curr.get('score', 0):.2f}  {_venue(curr)}[/dim]"
        )
        console.print(
            f"  [dim]Bedrock :[/dim] [green]{new['title'][:65]}[/green]\n"
            f"           [dim]score {new.get('score', 0):.2f}  {_venue(new)}[/dim]"
        )
        console.print(
            "  [dim](1)[/dim] Keep current as primary  +  add Bedrock as backup\n"
            "  [dim](2)[/dim] Switch to Bedrock primary  +  keep current as backup\n"
            "  [dim](3)[/dim] Keep only current  (ignore Bedrock)\n"
            "  [dim](4)[/dim] Take only Bedrock  (drop current)"
        )
        choice = click.prompt("  Choice", default="1").strip()

        if choice == "2":
            final.append(new)
            final.append({**curr, "backup": True,
                          "backup_note": f"Previous primary, replaced by Bedrock recommendation"})
        elif choice == "3":
            final.append(curr)
        elif choice == "4":
            final.append(new)
        else:  # 1 (default)
            final.append(curr)
            final.append({**new, "backup": True,
                          "backup_note": f"Bedrock alternative for: {curr['title'][:55]}"})

    # ── Additions: Bedrock added a new slot ───────────────────────────────────
    if additions:
        console.print(f"\n[bold green]+  {len(additions)} new slot(s) Bedrock recommends[/bold green]")

    for slot, new in additions:
        day, time = slot[:10], slot[11:16]
        console.print(
            f"\n  [bold]{_DAY_NAMES_PLAN.get(day, day)}  {time}[/bold]  "
            f"[green]{new['title'][:65]}[/green]  [dim](score {new.get('score', 0):.2f})[/dim]"
        )
        if click.confirm("  Add to schedule?", default=True):
            final.append(new)

    # ── Drops: Bedrock removed a slot ─────────────────────────────────────────
    if drops:
        console.print(f"\n[bold red]✕  {len(drops)} slot(s) Bedrock dropped[/bold red]")

    for slot, curr in drops:
        day, time = slot[:10], slot[11:16]
        console.print(
            f"\n  [bold]{_DAY_NAMES_PLAN.get(day, day)}  {time}[/bold]  "
            f"[cyan]{curr['title'][:65]}[/cyan]  [dim](score {curr.get('score', 0):.2f})[/dim]"
        )
        if click.confirm("  Keep in schedule?", default=True):
            final.append(curr)

    # ── Always preserve existing backups not already in final ─────────────────
    final_ids = {s["event_id"] for s in final}
    for b in existing_backups:
        if b["event_id"] not in final_ids:
            final.append(b)

    final.sort(key=lambda s: (s.get("start_date", ""), s.get("scheduled_start", "")))
    return final


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise click.ClickException(f"config.yaml not found — copy from config.yaml.example and fill in your preferences")
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _sessions_to_dict(schedule: list[ScheduledSession]) -> list[dict]:
    out = []
    for ss in schedule:
        s = ss.session
        entry = {
            "event_id": s.event_id,
            "title": s.title,
            "description": s.description[:300],
            "start_date": s.start_date.isoformat(),
            "start_time": s.start_time,
            "time_zone": s.time_zone,
            "location": s.location,
            "location_mode": s.location_mode,
            "learning_level": s.learning_level,
            "event_type": s.event_type,
            "score": s.score,
            "registration_url": s.registration_url,
            "learn_more_url": s.learn_more_url,
            "scheduled_start": ss.start.isoformat(),
            "scheduled_end": ss.end.isoformat(),
        }
        if ss.backup:
            entry["backup"] = True
        if ss.backup_note:
            entry["backup_note"] = ss.backup_note
        out.append(entry)
    return out


def _load_schedule(config: dict) -> list[ScheduledSession]:
    if not _SCHEDULE_PATH.exists():
        raise click.ClickException("schedule.json not found — run `awsevents plan` first")
    with open(_SCHEDULE_PATH) as f:
        data = json.load(f)

    from datetime import datetime
    from agent.catalog import Session
    from agent.scheduler import ScheduledSession

    sessions = []
    for item in data:
        s = Session(
            event_id=item["event_id"],
            title=item["title"],
            description=item.get("description", ""),
            start_date=date.fromisoformat(item["start_date"]),
            start_time=item.get("start_time"),
            time_zone=item.get("time_zone"),
            location=item.get("location"),
            location_mode=item.get("location_mode", "physical"),
            learning_level=item.get("learning_level"),
            event_type=item.get("event_type"),
            partner_name=None,
            registration_url=item.get("registration_url"),
            learn_more_url=item.get("learn_more_url"),
            score=float(item.get("score", 0)),
        )
        ss = ScheduledSession(
            session=s,
            day=item["start_date"],
            start=datetime.fromisoformat(item["scheduled_start"]),
            end=datetime.fromisoformat(item["scheduled_end"]),
            backup=bool(item.get("backup", False)),
            backup_note=item.get("backup_note", ""),
        )
        sessions.append(ss)
    return sessions


@click.group()
def cli() -> None:
    """re:Invent session planner — score, schedule, auto-register."""


# ── sync-wishlist ─────────────────────────────────────────────────────────────

@cli.command("sync-wishlist")
@click.option("--headless", is_flag=True, default=False, help="Run browser in headless mode (no visible window)")
def sync_wishlist_cmd(headless: bool) -> None:
    """Log in to registration.awsevents.com, scrape full catalog + your wishlist.

    Saves reinvent_catalog.json (all sessions) and wishlist.json (your bookmarks).
    Run this before 'plan' to use the real re:Invent session data instead of the
    public AWS Events catalog.
    """
    config = _load_config()
    reg_cfg = config.get("registration", {})
    email = reg_cfg.get("email") or ""
    password = os.getenv("AWSEVENTS_PASSWORD", "")

    if not email or not password:
        console.print("[red]Set registration.email in config.yaml and AWSEVENTS_PASSWORD in .env[/red]")
        return

    from agent.wishlist import sync_wishlist, save_results

    async def _run() -> None:
        console.print(f"[bold]Syncing re:Invent catalog from registration.awsevents.com...[/bold]")
        console.print(f"  Email: [cyan]{email}[/cyan]  |  Headless: {headless}")
        api_cfg = config.get("catalog_api", {})
        try:
            all_sessions, wishlisted = await sync_wishlist(
                email, password,
                headless=headless,
                rf_profile_id=api_cfg.get("rf_profile_id", ""),
                rf_widget_id=api_cfg.get("rf_widget_id", ""),
            )
        except RuntimeError as exc:
            msg = str(exc)
            if "CATALOG_NOT_OPEN" in msg:
                console.print("\n[yellow bold]⏳  Catalog not open yet[/yellow bold]")
                console.print("[yellow]" + msg.split("\n", 1)[-1] + "[/yellow]")
                console.print(
                    "\n[dim]Everything is ready — your preferences are configured in config.yaml.\n"
                    "When the catalog opens (~October 2026):\n"
                    "  1. Run [bold]awsevents sync-wishlist[/bold] — pulls all sessions + your wishlist\n"
                    "  2. Run [bold]awsevents plan[/bold]           — builds your optimised schedule\n"
                    "  3. Run [bold]awsevents watch[/bold]          — auto-registers when seats open[/dim]"
                )
            else:
                console.print(f"[red]Error:[/red] {exc}")
            return
        save_results(all_sessions, wishlisted)

        console.print(f"\n[bold green]Sync complete[/bold green]")
        console.print(f"  Total sessions in catalog : [cyan]{len(all_sessions)}[/cyan]")
        console.print(f"  Your wishlisted sessions  : [cyan]{len(wishlisted)}[/cyan]")
        if wishlisted:
            console.print("\n[bold]Your wishlist:[/bold]")
            for s in wishlisted[:20]:
                console.print(f"  [green]★[/green] {s.title[:70]}  [{s.level or '?'}]  {s.date or ''}")
        console.print("\n[dim]Run 'awsevents plan' to build your optimised schedule.[/dim]")

    asyncio.run(_run())


# ── plan ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--min-score", default=0.0, type=float, help="Only show sessions with score >= N (0-1)")
@click.option("--day", default=None, help="Filter to one day YYYY-MM-DD")
@click.option("--all-events", is_flag=True, default=False, help="Ignore date/location filters — plan from full catalog")
def plan(min_score: float, day: Optional[str], all_events: bool) -> None:
    """Score + schedule sessions. Uses scraped catalog (reinvent_catalog.json) when available,
    falls back to the public AWS Events API otherwise."""

    async def _run() -> None:
        config = _load_config()
        ev_cfg = config.get("event", {})
        scorer = _make_scorer(config)

        # ── Source selection ──────────────────────────────────────────────
        # Prefer the scraped re:Invent catalog when available (sync-wishlist was run).
        from pathlib import Path as _Path
        reinvent_catalog_path = _Path("reinvent_catalog.json")

        if reinvent_catalog_path.exists() and not all_events:
            from agent.wishlist import load_catalog
            reinvent_sessions = load_catalog()
            console.print(
                f"[bold]Using scraped re:Invent catalog[/bold]  "
                f"([cyan]{len(reinvent_sessions)}[/cyan] sessions from reinvent_catalog.json)"
            )
            wishlisted_count = sum(1 for s in reinvent_sessions if s.is_wishlisted)
            console.print(f"  [green]★ {wishlisted_count}[/green] wishlisted (locked score 1.0)  |  "
                          f"[dim]{len(reinvent_sessions) - wishlisted_count} to be scored by preferences[/dim]")
            scored = scorer.score_reinvent_sessions(reinvent_sessions)
        else:
            if not all_events:
                start = date.fromisoformat(ev_cfg["start_date"]) if ev_cfg.get("start_date") else None
                end = date.fromisoformat(ev_cfg["end_date"]) if ev_cfg.get("end_date") else None
                loc_text = ev_cfg.get("location_keyword")
                loc_mode = ev_cfg.get("location_mode")
            else:
                start, end, loc_text, loc_mode = None, None, None, None

            console.print(f"[bold]Fetching from public AWS Events catalog...[/bold]")
            console.print(f"[dim](Run 'awsevents sync-wishlist' first to use the full re:Invent session catalog)[/dim]")
            sessions = await fetch_sessions(
                start_date=start, end_date=end,
                location_text=loc_text, location_mode=loc_mode,
            )
            console.print(f"  Found [cyan]{len(sessions)}[/cyan] sessions")
            scored = scorer.score_all(sessions)

        above = [s for s in scored if s.score >= min_score]
        console.print(f"  [cyan]{len(above)}[/cyan] sessions with score >= {min_score}\n")

        if not above:
            console.print("[yellow]No sessions matched your preferences.[/yellow]")
            if not all_events and not reinvent_catalog_path.exists():
                console.print(
                    "[dim]re:Invent 2026 sessions open ~October 2026. "
                    "Run [bold]awsevents plan --all-events[/bold] to test with current data.[/dim]"
                )
            return

        schedule = build_schedule(above, config)
        if day:
            schedule = [ss for ss in schedule if ss.day == day]

        wishlisted_in_schedule = sum(1 for ss in schedule if ss.session.score == 1.0)
        console.print(f"[bold green]Schedule built:[/bold green] {len(schedule)} sessions across "
                      f"{len(set(ss.day for ss in schedule))} days  "
                      f"([green]★ {wishlisted_in_schedule} wishlisted[/green])\n")

        _print_schedule(schedule)

        new_sessions = _sessions_to_dict(schedule)

        # ── Approval gate: per-slot interactive review ─────────────────────────
        if _SCHEDULE_PATH.exists():
            with open(_SCHEDULE_PATH) as f:
                existing = json.load(f)
            result = _run_approval_gate(existing, new_sessions)
            if result is None:
                console.print("[dim]Schedule not saved — existing schedule unchanged.[/dim]")
                return
            new_sessions = result
        # No existing schedule — first run, save directly

        with open(_SCHEDULE_PATH, "w") as f:
            json.dump(new_sessions, f, indent=2)
        console.print(f"\n[dim]Schedule saved to {_SCHEDULE_PATH}[/dim]")

    asyncio.run(_run())


@cli.command("list")
@click.option("--min-score", default=0.1, type=float, help="Minimum relevance score (0-1)")
@click.option("--top", default=50, type=int, help="Show top N sessions")
@click.option("--all-events", is_flag=True, default=False, help="Ignore date/location filters — show all catalog sessions")
def list_sessions(min_score: float, top: int, all_events: bool) -> None:
    """List all matching sessions sorted by relevance score."""

    async def _run() -> None:
        config = _load_config()
        ev_cfg = config.get("event", {})

        if all_events:
            start, end, loc_text, loc_mode = None, None, None, None
        else:
            start = date.fromisoformat(ev_cfg["start_date"]) if ev_cfg.get("start_date") else None
            end = date.fromisoformat(ev_cfg["end_date"]) if ev_cfg.get("end_date") else None
            loc_text = ev_cfg.get("location_keyword")
            loc_mode = ev_cfg.get("location_mode")

        sessions = await fetch_sessions(
            start_date=start,
            end_date=end,
            location_text=loc_text if not all_events else None,
            location_mode=loc_mode if not all_events else None,
        )
        scorer = _make_scorer(config)
        scored = scorer.score_all(sessions)
        filtered = [s for s in scored if s.score >= min_score][:top]

        console.print(f"\n[bold]Top {len(filtered)} sessions[/bold]  (min score {min_score})\n")
        console.print(f"  {'SCORE':6}  {'DATE':10}  {'TIME':8}  {'LEVEL':13}  TITLE")
        console.print("  " + "─" * 90)
        for s in filtered:
            time_str = (s.start_time or "—")[:8]
            level_str = (s.learning_level or "—")[:13]
            title_str = s.title[:65]
            console.print(
                f"  [cyan]{s.score:.2f}[/cyan]   {s.start_date}  {time_str:<8}  {level_str:<13}  [bold]{title_str}[/bold]"
            )
            if s.location:
                console.print(f"           {'':10}   {'':8}  {'':13}  [dim]{s.location[:70]}[/dim]")

    asyncio.run(_run())


@cli.command()
def setup() -> None:
    """Interactive wizard: configure preferences, fetch catalog, review sessions, build schedule, export ICS."""
    from agent.wizard import run_wizard
    run_wizard()


@cli.command("schedule")
@click.option("--day", default=None, help="Show only this day, e.g. 2026-12-01")
def show_schedule(day: str | None) -> None:
    """Display saved schedule day-by-hour with venue and travel warnings."""
    from agent.wizard import print_schedule

    if not _SCHEDULE_PATH.exists():
        console.print("[red]schedule.json not found — run 'awsevents plan' or 'awsevents setup' first[/red]")
        return

    with open(_SCHEDULE_PATH) as f:
        data = json.load(f)

    if day:
        data = [s for s in data if s.get("start_date") == day]
        if not data:
            console.print(f"[yellow]No sessions found for {day}[/yellow]")
            return

    print_schedule(data)


@cli.command()
@click.option("--path", default="reinvent2026.ics", help="Output file path")
def export_ics(path: str) -> None:
    """Export schedule to an ICS calendar file for Apple Calendar / Google Calendar / Outlook."""
    from agent.wizard import export_ics as _export

    if not _SCHEDULE_PATH.exists():
        console.print("[red]schedule.json not found — run 'awsevents plan' or 'awsevents setup' first[/red]")
        return

    with open(_SCHEDULE_PATH) as f:
        data = json.load(f)

    out = Path(path)
    _export(data, out)
    console.print(f"[green]✓ Exported {len(data)} sessions to {out}[/green]")
    console.print("[dim]Import into Apple Calendar: File → Import | Google: Settings → Import & Export[/dim]")


@cli.command()
@click.option("--day", default=None, help="Day to edit, e.g. 2026-12-01 (skip to pick interactively)")
@click.option("--time", "time_slot", default=None, help="Time slot to replace, e.g. 13:00")
@click.option("--all", "show_all", is_flag=True, default=False, help="Show all matching sessions, not just top recommendations")
@click.option("--html", "with_html", is_flag=True, default=False, help="Open alternatives in browser")
def edit(day: str | None, time_slot: str | None, show_all: bool, with_html: bool) -> None:
    """Swap a session in your schedule. Pick a time slot, see recommended alternatives."""
    if not _SCHEDULE_PATH.exists():
        console.print("[red]schedule.json not found — run 'awsevents plan' or 'awsevents setup' first[/red]")
        return

    with open(_SCHEDULE_PATH) as f:
        schedule = json.load(f)

    config = _load_config()
    prefs = config.get("preferences", {})
    target_aoi = set(prefs.get("areas_of_interest", []))
    target_topics = set(prefs.get("topic_tracks", []))

    from agent.wizard import print_schedule, _DAY_NAMES, _HOP_MINUTES, _venue_cluster
    from agent.html_views import html_slot_alternatives, open_in_browser
    from datetime import datetime, timedelta

    # Pick day
    days = sorted(set(s.get("start_date", "") for s in schedule if s.get("start_date")))
    if not day:
        console.print("\n[bold]Days in your schedule:[/bold]")
        for i, d in enumerate(days, 1):
            sessions_on_day = [s for s in schedule if s.get("start_date") == d]
            console.print(f"  {i}. {_DAY_NAMES.get(d, d)}  ({len(sessions_on_day)} sessions)")
        raw = click.prompt("  Pick day (number or YYYY-MM-DD)", default="1")
        if raw.strip().isdigit():
            idx = int(raw.strip()) - 1
            day = days[idx] if 0 <= idx < len(days) else days[0]
        else:
            day = raw.strip()

    day_sessions = sorted(
        [s for s in schedule if s.get("start_date") == day],
        key=lambda x: x.get("scheduled_start") or x.get("start_time") or "",
    )

    # Show day's sessions and pick a slot
    console.print(f"\n[bold]{_DAY_NAMES.get(day, day)} — current schedule:[/bold]")
    for i, s in enumerate(day_sessions, 1):
        t = (s.get("scheduled_start") or "")[-8:-3] if s.get("scheduled_start") else (s.get("start_time") or "?")[:5]
        te = (s.get("scheduled_end") or "")[-8:-3] if s.get("scheduled_end") else "?"
        lvl = (s.get("learning_level") or "—")[:4]
        console.print(f"  {i}. {t}–{te}  [{lvl}]  {s['title'][:65]}")

    if not time_slot:
        raw = click.prompt("  Which slot to replace? (number or HH:MM)", default="1")
        if raw.strip().isdigit():
            idx = int(raw.strip()) - 1
            target_session = day_sessions[idx] if 0 <= idx < len(day_sessions) else None
        else:
            time_slot = raw.strip()
            target_session = next((s for s in day_sessions if (s.get("start_time") or "").startswith(time_slot[:5])), None)
    else:
        target_session = next((s for s in day_sessions if (s.get("start_time") or "").startswith(time_slot[:5])), None)

    if not target_session:
        console.print(f"[red]Session not found[/red]")
        return

    # Determine free window
    try:
        slot_start = datetime.fromisoformat(target_session["scheduled_start"])
        slot_end   = datetime.fromisoformat(target_session["scheduled_end"])
    except Exception:
        console.print("[red]Cannot parse session times[/red]")
        return

    console.print(f"\n  Replacing: [bold red]{target_session['title'][:70]}[/bold red]")
    console.print(f"  Window: {slot_start.strftime('%H:%M')} – {slot_end.strftime('%H:%M')}\n")

    # Load catalog and find alternatives for this slot
    catalog_path = Path("reinvent_catalog.json")
    if not catalog_path.exists():
        console.print("[red]reinvent_catalog.json not found — run 'awsevents sync-wishlist' first[/red]")
        return

    from agent.wishlist import load_catalog

    all_sessions = load_catalog()
    scorer = _make_scorer(config)
    scored_all = scorer.score_reinvent_sessions(all_sessions)

    scheduled_ids = {s["event_id"] for s in schedule if s["event_id"] != target_session["event_id"]}
    other_day_sessions = [s for s in schedule if s.get("start_date") == day and s["event_id"] != target_session["event_id"]]

    def _conflicts(candidate) -> bool:
        cand_start_str = candidate.start_time
        if not cand_start_str:
            return False
        try:
            cand_start = datetime.strptime(f"{day}T{cand_start_str}", "%Y-%m-%dT%H:%M")
        except Exception:
            return False
        dur = 60
        stype = (candidate.event_type or "").lower()
        if "workshop" in stype:
            dur = 120
        cand_end = cand_start + timedelta(minutes=dur)
        for other in other_day_sessions:
            try:
                o_start = datetime.fromisoformat(other["scheduled_start"])
                o_end   = datetime.fromisoformat(other["scheduled_end"])
                if max(cand_start, o_start) < min(cand_end, o_end):
                    return True
            except Exception:
                pass
        return False

    conf_days = {"2026-11-30","2026-12-01","2026-12-02","2026-12-03","2026-12-04","2026-12-05"}
    alternatives = [
        s for s in scored_all
        if s.event_id not in scheduled_ids
        and str(s.start_date) == day
        and str(s.start_date) in conf_days
        and not _conflicts(s)
    ]

    if not show_all:
        # Recommended: score >= 0.2, sorted by score, top 15
        alternatives = sorted([s for s in alternatives if s.score >= 0.2], key=lambda x: -x.score)[:15]
    else:
        alternatives = sorted(alternatives, key=lambda x: -x.score)

    if not alternatives:
        console.print("[yellow]No alternatives found for this slot that don't conflict with your other sessions.[/yellow]")
        return

    # Convert to dicts for HTML view
    alt_dicts = []
    for s in alternatives:
        alt_dicts.append({
            "event_id": s.event_id,
            "title": s.title,
            "description": s.description or "",
            "start_time": s.start_time,
            "location": s.location,
            "learning_level": s.learning_level,
            "session_type": s.event_type,
            "areas_of_interest": [],
            "score": s.score,
            "registration_url": s.registration_url,
        })

    time_window = f"{slot_start.strftime('%H:%M')}–{slot_end.strftime('%H:%M')}"

    if with_html:
        html = html_slot_alternatives(alt_dicts, day, time_window)
        path = open_in_browser(html, "edit_alternatives.html")
        console.print(f"  [dim]HTML view: file://{path}[/dim]")

    # Console list
    t = __import__("rich.table", fromlist=["Table"]).Table(box=__import__("rich", fromlist=["box"]).box.SIMPLE)
    t.add_column("#", width=4, style="dim")
    t.add_column("Score", width=6)
    t.add_column("Level", width=13)
    t.add_column("Time", width=6)
    t.add_column("Title", min_width=55)
    t.add_column("Venue", width=22)

    for i, s in enumerate(alternatives, 1):
        t.add_row(
            str(i),
            f"[cyan]{s.score:.2f}[/cyan]",
            s.learning_level or "—",
            s.start_time or "—",
            f"[bold]{s.title[:65]}[/bold]",
            (s.location or "—")[:22],
        )
    console.print(t)

    if not with_html and click.confirm("  Open in browser?", default=False):
        html = html_slot_alternatives(alt_dicts, day, time_window)
        open_in_browser(html, "edit_alternatives.html")

    console.print("[dim]Enter number to swap in, or 'q' to cancel[/dim]")
    raw = click.prompt("  Choice", default="q").strip()
    if raw == "q":
        return

    if raw.isdigit():
        pick = int(raw) - 1
        if 0 <= pick < len(alternatives):
            new_session = alternatives[pick]
            # Build replacement entry
            try:
                new_start = datetime.strptime(f"{day}T{new_session.start_time}", "%Y-%m-%dT%H:%M")
            except Exception:
                new_start = slot_start
            dur_min = 120 if "workshop" in (new_session.event_type or "").lower() else 60
            new_end = new_start + timedelta(minutes=dur_min)

            replacement = {
                "event_id": new_session.event_id,
                "title": new_session.title,
                "description": (new_session.description or "")[:300],
                "start_date": day,
                "start_time": new_session.start_time,
                "time_zone": new_session.time_zone,
                "location": new_session.location,
                "location_mode": new_session.location_mode,
                "learning_level": new_session.learning_level,
                "event_type": new_session.event_type,
                "score": new_session.score,
                "registration_url": new_session.registration_url,
                "learn_more_url": new_session.learn_more_url,
                "scheduled_start": new_start.isoformat(),
                "scheduled_end": new_end.isoformat(),
            }

            # Swap in schedule
            updated = [replacement if s["event_id"] == target_session["event_id"] else s for s in schedule]
            updated.sort(key=lambda x: (x.get("start_date", ""), x.get("scheduled_start") or ""))

            with open(_SCHEDULE_PATH, "w") as f:
                json.dump(updated, f, indent=2)

            console.print(f"\n  [green]✓ Swapped in:[/green] [bold]{new_session.title[:70]}[/bold]")
            console.print(f"  [dim]schedule.json updated. Run 'awsevents schedule' to review.[/dim]")


@cli.command()
@click.option("--port", default=8080, show_default=True, type=int, help="Local port for the web UI")
@click.option("--no-open", "no_open", is_flag=True, default=False, help="Don't auto-open browser")
def serve(port: int, no_open: bool) -> None:
    """Open the schedule in a browser with live edit controls.

    Starts a local web server so you can swap sessions, add backups,
    and remove sessions directly from the schedule view — no terminal
    prompts needed.

    \b
    Requires: uv sync --extra mcp  (pulls in uvicorn + starlette)
    """
    import uvicorn
    from agent.serve import create_app

    if not _SCHEDULE_PATH.exists():
        console.print("[red]schedule.json not found — run 'awsevents plan' first[/red]")
        return

    url = f"http://localhost:{port}"
    console.print(f"[bold]re:Invent Planner[/bold]  [dim]live edit UI[/dim]")
    console.print(f"  [cyan]{url}[/cyan]")
    console.print(f"  [dim]Ctrl+C to stop[/dim]\n")

    if not no_open:
        import threading
        threading.Timer(1.2, lambda: __import__("webbrowser").open(url)).start()

    uvicorn.run(create_app(port=port), host="127.0.0.1", port=port, log_level="warning")


@cli.command()
def register() -> None:
    """Register for sessions from saved schedule.json immediately."""
    _do_register(watch=False)


@cli.command()
def watch() -> None:
    """Watch for sessions to open, then auto-register. Polls until all done."""
    _do_register(watch=True)


def _do_register(watch: bool) -> None:
    config = _load_config()
    reg_cfg = config.get("registration", {})

    if not reg_cfg.get("enabled", False):
        console.print("[yellow]Registration is disabled in config.yaml (registration.enabled: false)[/yellow]")
        return

    email = reg_cfg.get("email") or ""
    password = os.getenv("AWSEVENTS_PASSWORD", "")

    if not email or not password:
        console.print("[red]Set registration.email in config.yaml and AWSEVENTS_PASSWORD in .env[/red]")
        return

    schedule = _load_schedule(config)
    console.print(f"[bold]Loaded schedule:[/bold] {len(schedule)} sessions")

    from agent.registrar import Registrar

    async def _run() -> None:
        reg = Registrar(reg_cfg, email=email, password=password)
        if watch:
            results = await reg.watch_and_register(schedule)
        else:
            results = await reg.register_schedule(schedule, watch=False)

        table = Table(title="Registration results", box=box.MINIMAL_DOUBLE_HEAD)
        table.add_column("Status", width=18)
        table.add_column("Session")
        for r in results:
            colour = {"registered": "green", "full": "red", "already_registered": "blue",
                      "not_open": "yellow", "error": "red"}.get(r.status, "white")
            table.add_row(f"[{colour}]{r.status}[/{colour}]", r.session.title[:80])
        console.print(table)

    asyncio.run(_run())


def _print_schedule(schedule: list[ScheduledSession]) -> None:
    current_day = None
    for ss in schedule:
        if ss.day != current_day:
            current_day = ss.day
            console.print(f"\n[bold underline]{ss.day}[/bold underline]")

        time_str = ss.start.strftime("%H:%M") if ss.start else "—"
        end_str = ss.end.strftime("%H:%M") if ss.end else "—"
        level = ss.session.learning_level or "—"
        loc = (ss.session.location or "—")[:30]
        score = f"{ss.session.score:.2f}"

        console.print(
            f"  [cyan]{time_str}–{end_str}[/cyan]  "
            f"[dim]{level:14s}[/dim]  "
            f"[green]{score}[/green]  "
            f"[bold]{ss.session.title[:65]}[/bold]"
        )
        if loc != "—":
            console.print(f"              [dim]{loc}[/dim]")
