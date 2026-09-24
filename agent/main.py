"""CLI entry point for the re:Invent session planner agent.

Commands:
  plan   — fetch sessions, score, build schedule, save + print
  list   — show all matching sessions with scores (no scheduling)
  watch  — load saved schedule and auto-register when seats open
  register — register from saved schedule immediately (no polling)
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
console = Console()

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


@cli.command()
@click.option("--min-score", default=0.0, type=float, help="Only show sessions with score >= N (0-1)")
@click.option("--day", default=None, help="Filter to one day YYYY-MM-DD")
def plan(min_score: float, day: Optional[str]) -> None:
    """Fetch sessions, score them, build optimised daily schedule and save to schedule.json."""

    async def _run() -> None:
        config = _load_config()
        ev_cfg = config.get("event", {})

        start = date.fromisoformat(ev_cfg["start_date"]) if ev_cfg.get("start_date") else None
        end = date.fromisoformat(ev_cfg["end_date"]) if ev_cfg.get("end_date") else None
        loc_text = ev_cfg.get("location_keyword")
        loc_mode = ev_cfg.get("location_mode")

        console.print(f"[bold]Fetching sessions from AWS Events catalog...[/bold]")
        sessions = await fetch_sessions(
            start_date=start,
            end_date=end,
            location_text=loc_text,
            location_mode=loc_mode,
        )
        console.print(f"  Found [cyan]{len(sessions)}[/cyan] sessions in date/location range")

        scorer = Scorer(config.get("preferences", {}))
        scored = scorer.score_all(sessions)
        above = [s for s in scored if s.score >= min_score]
        console.print(f"  [cyan]{len(above)}[/cyan] sessions with score >= {min_score}")

        if not above:
            console.print("[yellow]No sessions matched your preferences. Try relaxing config.yaml filters.[/yellow]")
            return

        schedule = build_schedule(above, config)
        if day:
            schedule = [ss for ss in schedule if ss.day == day]

        console.print(f"\n[bold green]Schedule built:[/bold green] {len(schedule)} sessions across "
                      f"{len(set(ss.day for ss in schedule))} days\n")

        _print_schedule(schedule)

        with open(_SCHEDULE_PATH, "w") as f:
            json.dump(_sessions_to_dict(schedule), f, indent=2)
        console.print(f"\n[dim]Schedule saved to {_SCHEDULE_PATH}[/dim]")

    asyncio.run(_run())


@cli.command("list")
@click.option("--min-score", default=0.1, type=float, help="Minimum relevance score (0-1)")
@click.option("--top", default=50, type=int, help="Show top N sessions")
def list_sessions(min_score: float, top: int) -> None:
    """List all matching sessions sorted by relevance score."""

    async def _run() -> None:
        config = _load_config()
        ev_cfg = config.get("event", {})

        start = date.fromisoformat(ev_cfg["start_date"]) if ev_cfg.get("start_date") else None
        end = date.fromisoformat(ev_cfg["end_date"]) if ev_cfg.get("end_date") else None

        sessions = await fetch_sessions(
            start_date=start,
            end_date=end,
            location_text=ev_cfg.get("location_keyword"),
            location_mode=ev_cfg.get("location_mode"),
        )
        scorer = Scorer(config.get("preferences", {}))
        scored = scorer.score_all(sessions)
        filtered = [s for s in scored if s.score >= min_score][:top]

        table = Table(title=f"Top {len(filtered)} sessions (min score {min_score})", box=box.MINIMAL_DOUBLE_HEAD)
        table.add_column("Score", style="cyan", width=6)
        table.add_column("Date", width=10)
        table.add_column("Time", width=8)
        table.add_column("Level", width=12)
        table.add_column("Location", width=20)
        table.add_column("Title", no_wrap=False)

        for s in filtered:
            table.add_row(
                f"{s.score:.2f}",
                s.start_date.isoformat(),
                s.start_time or "—",
                s.learning_level or "—",
                (s.location or "—")[:20],
                s.title[:80],
            )
        console.print(table)

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
