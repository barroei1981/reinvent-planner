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

load_dotenv()
console = Console(width=160)

_CONFIG_PATH = Path("config.yaml")
_SCHEDULE_PATH = Path("schedule.json")


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise click.ClickException(f"config.yaml not found — copy from config.yaml.example and fill in your preferences")
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _sessions_to_dict(schedule: list[ScheduledSession]) -> list[dict]:
    out = []
    for ss in schedule:
        s = ss.session
        out.append({
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
        })
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
        try:
            all_sessions, wishlisted = await sync_wishlist(email, password, headless=headless)
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
        scorer = Scorer(config.get("preferences", {}))

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

        with open(_SCHEDULE_PATH, "w") as f:
            json.dump(_sessions_to_dict(schedule), f, indent=2)
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
        scorer = Scorer(config.get("preferences", {}))
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
