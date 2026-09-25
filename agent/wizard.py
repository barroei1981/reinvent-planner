"""Interactive setup wizard and schedule display for re:Invent session planner."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text

console = Console(width=160)

_CONFIG_PATH = Path("config.yaml")
_ENV_PATH = Path(".env")
_CATALOG_PATH = Path("reinvent_catalog.json")
_SCHEDULE_PATH = Path("schedule.json")

_KNOWN_AOI = [
    "Generative AI", "Agentic AI", "Machine Learning", "Data Analytics",
    "DevSecOps", "Application Security", "Cloud Operations", "Cost Optimization",
    "Developer Experience", "Serverless", "Containers & Microservices",
    "Database", "Storage", "Networking & Content Delivery",
    "Migration & Modernization", "Resilience",
]

_KNOWN_TOPICS = [
    "Artificial Intelligence", "Analytics", "Security, Identity, & Compliance",
    "Developer Tools", "Serverless", "Compute", "Containers",
    "Database", "Storage", "Networking & Content Delivery",
    "Management & Governance", "Migration", "Application Integration",
]

_KNOWN_LEVELS = ["Foundational", "Intermediate", "Advanced", "Expert"]

_SESSION_TYPES = [
    "Breakout Session", "Chalk Talk", "Workshop", "Builder Session",
    "Code Talk", "Innovation Talk", "Leadership Session", "Lightning Talk",
]

# Approximate travel time (minutes) between clusters
_HOP_MINUTES: dict[tuple[int, int], int] = {
    (1, 2): 15, (2, 1): 15,
    (1, 3): 25, (3, 1): 25,
    (1, 4): 35, (4, 1): 35,
    (2, 3): 20, (3, 2): 20,
    (2, 4): 30, (4, 2): 30,
    (3, 4): 20, (4, 3): 20,
}

_CLUSTER_KEYWORDS: list[list[str]] = [
    [],  # index 0 unused
    ["venetian", "wynn", "encore", "palazzo"],
    ["caesars", "forum", "linq", "harrahs", "paris"],
    ["mgm", "park mgm", "aria"],
    ["mandalay", "delano"],
]

_CLUSTER_LABELS = ["", "Venetian/Wynn", "Caesars/LINQ", "MGM Grand", "Mandalay Bay"]

_DAY_NAMES = {
    "2026-11-30": "Sun Nov 30",
    "2026-12-01": "Mon Dec 1",
    "2026-12-02": "Tue Dec 2",
    "2026-12-03": "Wed Dec 3",
    "2026-12-04": "Thu Dec 4",
    "2026-12-05": "Fri Dec 5",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _venue_cluster(location: str | None) -> int:
    if not location:
        return 0
    loc = location.lower()
    for i, kws in enumerate(_CLUSTER_KEYWORDS[1:], start=1):
        if any(kw in loc for kw in kws):
            return i
    return 0


def _multiselect(options: list[str], prompt: str, current: list[str] | None = None) -> list[str]:
    """Show a numbered list; user enters comma-separated numbers. Returns selected items."""
    console.print(f"\n[bold]{prompt}[/bold]")
    for i, opt in enumerate(options, 1):
        marker = "[green]✓[/green] " if current and opt in current else "  "
        console.print(f"  {marker}[dim]{i:2d}.[/dim] {opt}")
    if current:
        console.print(f"\n  [dim]Current: {', '.join(current)}[/dim]")
    console.print("  [dim]Enter numbers separated by commas, or press Enter to keep current.[/dim]")

    raw = click.prompt("  Selection", default="", show_default=False).strip()
    if not raw:
        return current or []

    selected = []
    for part in re.split(r"[,\s]+", raw):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(options):
                selected.append(options[idx])
    return selected if selected else (current or [])


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_config(config: dict) -> None:
    with open(_CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _update_env(key: str, value: str) -> None:
    """Write or update a key in .env."""
    lines: list[str] = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text().splitlines()

    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")

    _ENV_PATH.write_text("\n".join(lines) + "\n")


# ── Step 0: User details ───────────────────────────────────────────────────────

def step_user_details(config: dict) -> dict:
    console.print(Panel("[bold cyan]Step 0 — User Details[/bold cyan]\nYour re:Invent registration credentials.", expand=False))

    reg = config.setdefault("registration", {})

    current_email = reg.get("email", "")
    email = click.prompt("  Email address", default=current_email or "").strip()
    reg["email"] = email

    current_pw = os.getenv("AWSEVENTS_PASSWORD", "")
    console.print(f"  Password  [dim](stored in .env only — never committed)[/dim]")
    if current_pw:
        change = click.confirm("  Password already set in .env. Change it?", default=False)
        if change:
            password = getpass.getpass("  New password: ")
            if password:
                _update_env("AWSEVENTS_PASSWORD", password)
                console.print("  [green]✓ Password saved to .env[/green]")
    else:
        password = getpass.getpass("  Password: ")
        if password:
            _update_env("AWSEVENTS_PASSWORD", password)
            console.print("  [green]✓ Password saved to .env[/green]")
        else:
            console.print("  [yellow]No password entered — registration commands will be unavailable[/yellow]")

    if not reg.get("enabled"):
        reg["enabled"] = True
    if not reg.get("watch_interval_seconds"):
        reg["watch_interval_seconds"] = 30
    if not reg.get("max_retries"):
        reg["max_retries"] = 3
    if not reg.get("max_sessions_to_register"):
        reg["max_sessions_to_register"] = 25

    _save_config(config)
    console.print("  [green]✓ User details saved to config.yaml[/green]")
    return config


# ── Step 1: Configure interests ────────────────────────────────────────────────

def step_configure_interests(config: dict) -> dict:
    console.print(Panel("[bold cyan]Step 1 — Configure Interests[/bold cyan]\nChoose the topics and session types that matter to you.", expand=False))

    prefs = config.setdefault("preferences", {})

    # Areas of interest
    prefs["areas_of_interest"] = _multiselect(
        _KNOWN_AOI,
        "Areas of Interest  [dim](sessions in these areas get a high relevance bonus)[/dim]",
        current=prefs.get("areas_of_interest", []),
    )

    # Topic tracks
    prefs["topic_tracks"] = _multiselect(
        _KNOWN_TOPICS,
        "Topic Tracks  [dim](secondary signal for session scoring)[/dim]",
        current=prefs.get("topic_tracks", []),
    )

    # Learning levels
    console.print("\n[bold]Learning Levels[/bold]")
    console.print("  1. Preferred  — sessions you actively want  (score × 1.0)")
    console.print("  2. Acceptable — sessions you're OK with     (score × 0.6)")
    console.print("  3. Avoid      — sessions to de-prioritise   (score × 0.2)")

    levels_cfg = prefs.setdefault("levels", {})

    preferred = _multiselect(
        _KNOWN_LEVELS, "Preferred levels",
        current=levels_cfg.get("preferred", ["Advanced", "Expert"]),
    )
    acceptable = _multiselect(
        _KNOWN_LEVELS, "Acceptable levels",
        current=levels_cfg.get("acceptable", ["Intermediate"]),
    )
    avoid = [lvl for lvl in _KNOWN_LEVELS if lvl not in preferred and lvl not in acceptable]

    levels_cfg["preferred"] = preferred
    levels_cfg["acceptable"] = acceptable
    levels_cfg["avoid"] = avoid
    console.print(f"  [dim]Auto-avoid: {', '.join(avoid) or 'none'}[/dim]")

    # Session types
    prefs["session_types"] = _multiselect(
        _SESSION_TYPES,
        "Preferred session formats  [dim](others get a mild 0.7 score penalty)[/dim]",
        current=prefs.get("session_types", ["Breakout Session", "Chalk Talk", "Workshop"]),
    )

    # Max sessions per day
    current_max = prefs.get("max_sessions_per_day", 6)
    max_per_day = click.prompt(
        f"  Max sessions per day",
        default=current_max, type=int,
    )
    prefs["max_sessions_per_day"] = max_per_day

    _save_config(config)
    console.print("\n  [green]✓ Preferences saved to config.yaml[/green]")
    return config


# ── Step 2: Fetch & score catalog ─────────────────────────────────────────────

async def step_fetch_catalog(config: dict) -> list:
    console.print(Panel("[bold cyan]Step 2 — Fetch & Score Catalog[/bold cyan]", expand=False))

    reg = config.get("registration", {})
    email = reg.get("email", "")
    password = os.getenv("AWSEVENTS_PASSWORD", "")

    from agent.wishlist import sync_wishlist, save_results, load_catalog
    from agent.scorer import Scorer

    if _CATALOG_PATH.exists():
        use_cache = click.confirm(
            f"  reinvent_catalog.json already exists. Use cached catalog?",
            default=True,
        )
        if use_cache:
            sessions = load_catalog()
            console.print(f"  [green]✓ Loaded {len(sessions)} sessions from cache[/green]")
        else:
            sessions = await _fetch_fresh(email, password)
            save_results(sessions, [s for s in sessions if s.is_wishlisted])
    else:
        sessions = await _fetch_fresh(email, password)
        if sessions:
            save_results(sessions, [s for s in sessions if s.is_wishlisted])

    if not sessions:
        return []

    scorer = Scorer(config.get("preferences", {}))
    scored = scorer.score_reinvent_sessions(sessions)
    console.print(f"  [green]✓ Scored {len(scored)} sessions[/green]")
    return scored


async def _fetch_fresh(email: str, password: str) -> list:
    from agent.wishlist import sync_wishlist
    console.print(f"  Fetching from catalog.awsevents.com…")
    try:
        all_sessions, _ = await sync_wishlist(email, password, headless=True)
        console.print(f"  [green]✓ Fetched {len(all_sessions)} sessions[/green]")
        return all_sessions
    except RuntimeError as exc:
        if "CATALOG_NOT_OPEN" in str(exc):
            console.print("  [yellow]Catalog not open yet — using cached data if available[/yellow]")
        else:
            console.print(f"  [red]Fetch error:[/red] {exc}")
        return []


# ── HTML offer helper ─────────────────────────────────────────────────────────

def _offer_html(html: str, filename: str, label: str) -> None:
    """Offer to open an HTML view. Does nothing if user declines."""
    if click.confirm(f"  Open [bold]{label}[/bold] in browser?", default=True):
        from agent.html_views import open_in_browser
        path = open_in_browser(html, filename)
        console.print(f"  [dim]HTML view: file://{path}[/dim]")


# ── Step 3: Recommendations review ────────────────────────────────────────────

def step_recommendations_review(scored_sessions: list) -> set[str]:
    """Show top sessions; return a set of event_ids the user wants to EXCLUDE."""
    console.print(Panel(
        "[bold cyan]Step 3 — Recommendations Review[/bold cyan]\n"
        "Review the top-scoring sessions. Mark any you want to exclude from scheduling.",
        expand=False,
    ))

    top = [s for s in scored_sessions if s.score >= 0.25][:60]

    if not top:
        console.print("  [dim]No sessions to review.[/dim]")
        return set()

    # HTML view — open first so user can browse while working through console
    from agent.html_views import html_recommendations
    _offer_html(html_recommendations(top), "step3_recommendations.html", "Recommendations (visual)")

    # Check for saved exclusions from HTML download
    excluded: set[str] = set()
    excl_file = Path("exclusions.json")
    if excl_file.exists():
        try:
            saved = json.loads(excl_file.read_text())
            if isinstance(saved, list) and saved:
                excluded = set(saved)
                console.print(f"  [green]✓ Loaded {len(excluded)} exclusions from exclusions.json[/green]")
                if click.confirm("  Use these saved exclusions?", default=True):
                    excl_file.unlink(missing_ok=True)
                    return excluded
                excluded.clear()
        except Exception:
            pass

    page_size = 10
    i = 0

    while i < len(top):
        page = top[i:i + page_size]
        t = Table(box=box.SIMPLE, show_header=True, header_style="bold")
        t.add_column("#", width=4, style="dim")
        t.add_column("Score", width=6)
        t.add_column("Level", width=13)
        t.add_column("Date", width=10)
        t.add_column("Time", width=6)
        t.add_column("Title", min_width=50)
        t.add_column("Venue", width=18)

        for j, s in enumerate(page, start=i + 1):
            star = "★ " if s.score == 1.0 else "  "
            ex = "[red]✗[/red] " if s.event_id in excluded else "  "
            t.add_row(
                f"{ex}{j}",
                f"[cyan]{s.score:.2f}[/cyan]",
                s.learning_level or "—",
                str(s.start_date) if s.start_date else "—",
                s.start_time or "—",
                f"{star}[bold]{s.title[:65]}[/bold]",
                (s.location or "—")[:18],
            )
        console.print(t)

        console.print("[dim]Enter numbers to exclude (comma-separated), n=next, p=prev, d=done, a=accept all[/dim]")
        raw = click.prompt(f"  Page {i // page_size + 1}/{(len(top) - 1) // page_size + 1}", default="n", show_default=False).strip().lower()

        if raw == "d" or raw == "":
            break
        elif raw == "a":
            excluded.clear()
            break
        elif raw == "n":
            i += page_size
        elif raw == "p":
            i = max(0, i - page_size)
        else:
            for part in re.split(r"[,\s]+", raw):
                part = part.strip()
                if part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(top):
                        eid = top[idx].event_id
                        if eid in excluded:
                            excluded.discard(eid)
                            console.print(f"  [green]Restored:[/green] {top[idx].title[:60]}")
                        else:
                            excluded.add(eid)
                            console.print(f"  [red]Excluded:[/red] {top[idx].title[:60]}")

    # Refresh HTML with exclusions applied
    if excluded:
        from agent.html_views import html_recommendations, open_in_browser
        open_in_browser(html_recommendations(top, excluded), "step3_recommendations.html")

    console.print(f"  [green]✓ {len(excluded)} sessions excluded[/green]" if excluded else "  [green]✓ No exclusions[/green]")
    return excluded


# ── Step 4: Conflict resolver ──────────────────────────────────────────────────

def step_conflict_resolver(schedule: list[dict]) -> list[dict]:
    """Surface time conflicts in the proposed schedule and let the user resolve them."""
    console.print(Panel("[bold cyan]Step 4 — Conflict Resolver[/bold cyan]\nReview sessions that overlap in time.", expand=False))

    by_day: dict[str, list[dict]] = {}
    for s in schedule:
        by_day.setdefault(s.get("start_date", ""), []).append(s)

    conflicts: list[tuple[dict, dict]] = []
    for day, sessions in by_day.items():
        ordered = sorted(sessions, key=lambda x: x.get("scheduled_start") or "")
        for j in range(len(ordered) - 1):
            a = ordered[j]
            b = ordered[j + 1]
            a_end = a.get("scheduled_end", "")
            b_start = b.get("scheduled_start", "")
            if a_end and b_start and a_end > b_start:
                conflicts.append((a, b))

    if not conflicts:
        console.print("  [green]✓ No conflicts found — schedule is clean[/green]")
        return schedule

    console.print(f"  Found [yellow]{len(conflicts)}[/yellow] overlapping session pair(s).\n")

    # HTML view of all conflicts for reference
    from agent.html_views import html_conflicts
    _offer_html(html_conflicts(conflicts), "step4_conflicts.html", "Conflict Resolver (visual)")

    to_remove: set[str] = set()

    for idx, (a, b) in enumerate(conflicts, 1):
        if a["event_id"] in to_remove or b["event_id"] in to_remove:
            continue

        console.print(f"  [bold yellow]Conflict {idx}[/bold yellow]  —  {_DAY_NAMES.get(a.get('start_date',''), a.get('start_date',''))}")

        t = Table(box=box.SIMPLE, show_header=True, header_style="bold")
        t.add_column("Option", width=8)
        t.add_column("Time", width=13)
        t.add_column("Score", width=7)
        t.add_column("Level", width=13)
        t.add_column("Type", width=18)
        t.add_column("Title")

        a_start = (a.get("scheduled_start") or "")[-8:-3] if a.get("scheduled_start") else "?"
        a_end_t = (a.get("scheduled_end") or "")[-8:-3] if a.get("scheduled_end") else "?"
        b_start = (b.get("scheduled_start") or "")[-8:-3] if b.get("scheduled_start") else "?"
        b_end_t = (b.get("scheduled_end") or "")[-8:-3] if b.get("scheduled_end") else "?"

        t.add_row(
            "[cyan]1[/cyan]", f"{a_start}–{a_end_t}",
            f"{a.get('score', 0):.2f}", a.get("learning_level") or "—",
            (a.get("event_type") or "—")[:16], a["title"][:65],
        )
        t.add_row(
            "[cyan]2[/cyan]", f"{b_start}–{b_end_t}",
            f"{b.get('score', 0):.2f}", b.get("learning_level") or "—",
            (b.get("event_type") or "—")[:16], b["title"][:65],
        )
        console.print(t)

        choice = click.prompt("  Keep which? (1/2/both)", default="1", show_default=True).strip()
        if choice == "1":
            to_remove.add(b["event_id"])
            console.print(f"  [green]Kept:[/green] {a['title'][:60]}")
        elif choice == "2":
            to_remove.add(a["event_id"])
            console.print(f"  [green]Kept:[/green] {b['title'][:60]}")
        else:
            console.print("  [dim]Keeping both — you'll need to manage the overlap manually[/dim]")
        console.print()

    remaining = [s for s in schedule if s["event_id"] not in to_remove]
    console.print(f"  [green]✓ {len(to_remove)} sessions removed. Schedule has {len(remaining)} sessions.[/green]")
    return remaining


# ── Step 5 + standalone: Schedule view ────────────────────────────────────────

def print_schedule(schedule: list[dict], offer_html: bool = True) -> None:
    """Day-by-hour schedule with venue and travel warnings."""
    if offer_html:
        from agent.html_views import html_schedule, open_in_browser
        html = html_schedule(schedule)
        _offer_html(html, "step5_schedule.html", "Schedule (visual, day tabs)")

    sorted_sessions = sorted(
        schedule,
        key=lambda x: (x.get("start_date", ""), x.get("scheduled_start") or x.get("start_time") or "99:99"),
    )

    by_day: dict[str, list[dict]] = {}
    for s in sorted_sessions:
        by_day.setdefault(s.get("start_date", "unknown"), []).append(s)

    for day, sessions in sorted(by_day.items()):
        day_label = _DAY_NAMES.get(day, day)
        total = len(sessions)
        clusters = set(_venue_cluster(s.get("location")) for s in sessions if _venue_cluster(s.get("location")))
        cluster_labels = " → ".join(_CLUSTER_LABELS[c] for c in sorted(clusters) if c > 0)
        console.print(f"\n[bold reverse] {day_label}  —  {total} sessions  {cluster_labels} [/bold reverse]")
        console.print()

        prev_end: datetime | None = None
        prev_cluster: int = 0

        for s in sessions:
            title = s.get("title", "")
            stype = (s.get("event_type") or "")
            level = s.get("learning_level") or "—"
            location = s.get("location") or "—"
            score = s.get("score", 0)
            is_wishlist = s.get("is_wishlisted", False) or score == 1.0
            is_keynote = stype == "Keynote"
            is_open = s.get("notes") and "open attendance" in (s.get("notes") or "").lower()

            # Parse times
            ss = s.get("scheduled_start")
            se = s.get("scheduled_end")
            try:
                start_dt = datetime.fromisoformat(ss) if ss else None
            except Exception:
                start_dt = None
            try:
                end_dt = datetime.fromisoformat(se) if se else None
            except Exception:
                end_dt = None

            start_str = start_dt.strftime("%H:%M") if start_dt else (s.get("start_time") or "?")[:5]
            end_str = end_dt.strftime("%H:%M") if end_dt else "?"

            curr_cluster = _venue_cluster(location)

            # Travel warning
            if prev_end and start_dt and prev_cluster and curr_cluster and prev_cluster != curr_cluster:
                gap_min = int((start_dt - prev_end).total_seconds() / 60)
                travel_min = _HOP_MINUTES.get((prev_cluster, curr_cluster), 25)
                from_lbl = _CLUSTER_LABELS[prev_cluster]
                to_lbl = _CLUSTER_LABELS[curr_cluster]
                if gap_min < travel_min:
                    console.print(
                        f"  [red bold]⚠  {from_lbl} → {to_lbl}  |  {travel_min} min travel, only {gap_min} min gap — TIGHT[/red bold]"
                    )
                else:
                    console.print(
                        f"  [yellow]→  {from_lbl} → {to_lbl}  |  {travel_min} min travel, {gap_min} min gap ✓[/yellow]"
                    )

            # Session row
            star = "[green]★[/green] " if is_wishlist else "  "
            keynote_tag = " [bold magenta][KEYNOTE][/bold magenta]" if is_keynote else ""
            open_tag = " [dim](open attendance)[/dim]" if is_open or is_keynote else ""
            level_color = {"Expert": "red", "Advanced": "yellow", "Intermediate": "cyan", "Foundational": "blue"}.get(level, "white")

            console.print(
                f"  {star}[cyan]{start_str}–{end_str}[/cyan]  "
                f"[{level_color}]{level:13s}[/{level_color}]  "
                f"[bold]{title[:70]}[/bold]{keynote_tag}{open_tag}"
            )
            short_loc = location.split("|")[0].strip()[:35]
            console.print(f"               [dim]{short_loc:<35}  {stype[:20]}[/dim]")

            if s.get("notes") and not is_keynote:
                console.print(f"               [dim italic]{s['notes'][:80]}[/dim italic]")

            prev_end = end_dt
            prev_cluster = curr_cluster if curr_cluster else prev_cluster

        console.print()


# ── Step 6: Export + registration monitoring ──────────────────────────────────

def export_ics(schedule: list[dict], path: Path) -> None:
    """Write schedule to an ICS calendar file."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AWS re:Invent 2026 Planner//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:re:Invent 2026",
        "X-WR-TIMEZONE:America/Los_Angeles",
    ]

    for s in schedule:
        ss = s.get("scheduled_start", "")
        se = s.get("scheduled_end", "")
        if not ss:
            continue

        try:
            start_dt = datetime.fromisoformat(ss)
            end_dt = datetime.fromisoformat(se) if se else start_dt + timedelta(hours=1)
        except Exception:
            continue

        dtstart = start_dt.strftime("%Y%m%dT%H%M%S")
        dtend = end_dt.strftime("%Y%m%dT%H%M%S")
        uid = f"{s.get('event_id', 'unknown')}@awsevents.reinvent2026"
        summary = _ics_escape(s.get("title", ""))
        location = _ics_escape((s.get("location") or "").split("|")[0].strip())
        desc_parts = []
        if s.get("learning_level"):
            desc_parts.append(f"Level: {s['learning_level']}")
        if s.get("event_type"):
            desc_parts.append(f"Type: {s['event_type']}")
        if s.get("score"):
            desc_parts.append(f"Score: {s['score']:.2f}")
        if s.get("notes"):
            desc_parts.append(s["notes"])
        if s.get("registration_url"):
            desc_parts.append(f"Register: {s['registration_url']}")
        description = _ics_escape(" | ".join(desc_parts))

        lines += [
            "BEGIN:VEVENT",
            f"DTSTART;TZID=America/Los_Angeles:{dtstart}",
            f"DTEND;TZID=America/Los_Angeles:{dtend}",
            f"UID:{uid}",
            f"SUMMARY:{summary}",
            f"LOCATION:{location}",
            f"DESCRIPTION:{description}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    path.write_text("\r\n".join(lines) + "\r\n")


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _dicts_to_scheduled(dicts: list[dict]):
    """Convert schedule dicts back to ScheduledSession objects for the registrar."""
    from agent.catalog import Session as CSession
    from agent.scheduler import ScheduledSession
    from datetime import date as dt_date

    result = []
    for item in dicts:
        try:
            start_date = dt_date.fromisoformat(item["start_date"]) if item.get("start_date") else dt_date(2026, 11, 30)
        except Exception:
            start_date = dt_date(2026, 11, 30)
        s = CSession(
            event_id=item["event_id"],
            title=item["title"],
            description=item.get("description", ""),
            start_date=start_date,
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
        try:
            start_dt = datetime.fromisoformat(item["scheduled_start"])
            end_dt = datetime.fromisoformat(item["scheduled_end"])
        except Exception:
            continue
        result.append(ScheduledSession(session=s, day=item.get("start_date", ""), start=start_dt, end=end_dt))
    return result


# ── Step 6: Export + launch registration ──────────────────────────────────────

def step_export_and_register(schedule_dicts: list[dict], config: dict) -> list[dict] | None:
    """Save ICS, save schedule.json, optionally start registration. Returns results or None."""
    console.print(Panel(
        "[bold cyan]Step 6 — Export & Registration[/bold cyan]\n"
        "Save your schedule, export to calendar, and optionally register for sessions now.",
        expand=False,
    ))

    if click.confirm("  Save schedule.json?", default=True):
        _SCHEDULE_PATH.write_text(json.dumps(schedule_dicts, indent=2))
        console.print(f"  [green]✓ Saved to {_SCHEDULE_PATH}[/green]")

    if click.confirm("  Export to calendar (reinvent2026.ics)?", default=True):
        ics_path = Path("reinvent2026.ics")
        export_ics(schedule_dicts, ics_path)
        console.print(f"  [green]✓ Exported to {ics_path}[/green]")
        console.print("  [dim]Apple Calendar: File → Import | Google: Settings → Import & Export[/dim]")

    reg_cfg = config.get("registration", {})
    if not reg_cfg.get("enabled"):
        console.print("  [yellow]Registration disabled in config.yaml — skipping[/yellow]")
        return None

    email = reg_cfg.get("email", "")
    password = os.getenv("AWSEVENTS_PASSWORD", "")
    if not email or not password:
        console.print("  [yellow]Email or password not set — skipping registration[/yellow]")
        return None

    # Skip keynotes (open attendance)
    registrable = [s for s in schedule_dicts if s.get("event_type") != "Keynote" and s.get("registration_url")]

    if not registrable:
        console.print("  [yellow]No sessions with registration URLs — nothing to register[/yellow]")
        return None

    console.print(f"\n  [bold]{len(registrable)} sessions[/bold] eligible for registration.")
    console.print("  1. Register now  — attempt all sessions immediately")
    console.print("  2. Watch mode    — poll until sessions open, then auto-register")
    console.print("  3. Skip          — register manually later with 'awsevents register' / 'awsevents watch'")
    mode = click.prompt("  Choose", default="2", show_default=True).strip()

    if mode == "3":
        console.print("  [dim]Run 'awsevents register' or 'awsevents watch' when ready.[/dim]")
        return None

    from agent.registrar import Registrar
    ss_list = _dicts_to_scheduled(registrable)
    reg = Registrar(reg_cfg, email=email, password=password)
    watch = (mode == "2")

    console.print(f"\n  [bold]Starting {'watch + ' if watch else ''}registration for {len(ss_list)} sessions...[/bold]")
    console.print("  [dim]Ctrl+C to stop. Partial results are saved automatically.[/dim]\n")

    results_raw = asyncio.run(_run_registration(reg, ss_list, watch=watch))

    results: list[dict] = []
    for r in results_raw:
        results.append({
            "event_id": r.session.event_id,
            "title": r.session.title,
            "status": r.status,
            "error": getattr(r, "error", None),
        })

    Path("registration_results.json").write_text(json.dumps(results, indent=2))
    console.print(f"  [dim]Registration results saved to registration_results.json[/dim]")
    return results


async def _run_registration(reg, ss_list: list, watch: bool) -> list:
    if watch:
        return await reg.watch_and_register(ss_list)
    return await reg.register_schedule(ss_list, watch=False)


# ── Step 7: Registration status ────────────────────────────────────────────────

def step_registration_status(results: list[dict], schedule: list[dict]) -> None:
    console.print(Panel(
        "[bold cyan]Step 7 — Registration Status[/bold cyan]\n"
        "Review what was registered, what's walk-in eligible, and what was missed.",
        expand=False,
    ))

    registered = [r for r in results if r.get("status") in ("registered", "already_registered")]
    full_missed = [r for r in results if r.get("status") == "full"]
    not_open    = [r for r in results if r.get("status") == "not_open"]
    errors      = [r for r in results if r.get("status") == "error"]

    console.print(f"\n  [green]✓ Registered:     {len(registered)}[/green]")
    console.print(f"  [yellow]○ Not open yet:   {len(not_open)}[/yellow]")
    console.print(f"  [red]✗ Full / missed:  {len(full_missed)}[/red]")
    if errors:
        console.print(f"  [red]! Errors:         {len(errors)}[/red]")

    if full_missed:
        console.print("\n  [bold yellow]Walk-in strategy for full sessions:[/bold yellow]")
        console.print("  [dim]Arrive 5–10 min before start, join the walk-in line at the room entrance.[/dim]")
        for r in full_missed:
            sched = next((s for s in schedule if s.get("event_id") == r.get("event_id")), {})
            time_str = (sched.get("start_time") or sched.get("scheduled_start", "")[-8:-3])[:5]
            day = sched.get("start_date", "")
            console.print(
                f"  [yellow]↯[/yellow]  {_DAY_NAMES.get(day, day)}  {time_str}  {r['title'][:65]}"
            )

    _print_gaps(schedule, {r["event_id"] for r in registered})

    # HTML status dashboard
    from agent.html_views import html_registration_status
    _offer_html(html_registration_status(results, schedule), "step7_status.html", "Registration Status (visual)")


def _print_gaps(schedule: list[dict], registered_ids: set[str]) -> None:
    keynote_ids = {s["event_id"] for s in schedule if s.get("event_type") == "Keynote"}
    secured_ids = registered_ids | keynote_ids

    by_day: dict[str, list[dict]] = {}
    for s in schedule:
        by_day.setdefault(s.get("start_date", ""), []).append(s)

    gaps_found = False
    for day in sorted(by_day):
        secured = sorted(
            [s for s in by_day[day] if s["event_id"] in secured_ids],
            key=lambda x: x.get("scheduled_start") or "",
        )
        prev_end = None
        for s in secured:
            try:
                s_dt = datetime.fromisoformat(s["scheduled_start"]) if s.get("scheduled_start") else None
                e_dt = datetime.fromisoformat(s["scheduled_end"]) if s.get("scheduled_end") else None
            except Exception:
                s_dt = e_dt = None
            if prev_end and s_dt and s_dt > prev_end:
                gap = int((s_dt - prev_end).total_seconds() / 60)
                if gap >= 60:
                    if not gaps_found:
                        console.print("\n  [bold]Free slots in your secured schedule:[/bold]")
                        gaps_found = True
                    console.print(
                        f"  [dim]{_DAY_NAMES.get(day, day)}  "
                        f"{prev_end.strftime('%H:%M')}–{s_dt.strftime('%H:%M')}  ({gap} min)[/dim]"
                        f"  → [bold]awsevents edit[/bold] to fill"
                    )
            prev_end = e_dt


# ── Main wizard orchestrator ───────────────────────────────────────────────────

def run_wizard() -> None:
    console.print(Panel(
        "[bold cyan]re:Invent 2026 Session Planner — Setup Wizard[/bold cyan]\n\n"
        "This wizard will configure your preferences, fetch the session catalog,\n"
        "help you review recommendations, resolve conflicts, and export your schedule.",
        expand=False,
    ))

    config = _load_config()

    # Step 0: user details
    config = step_user_details(config)

    # Step 1: interests
    config = step_configure_interests(config)

    # Step 2: fetch + score
    scored = asyncio.run(step_fetch_catalog(config))
    if not scored:
        console.print("[yellow]No sessions available. Re-run after syncing the catalog.[/yellow]")
        return

    # Step 3: recommendations review
    excluded_ids = step_recommendations_review(scored)
    filtered = [s for s in scored if s.event_id not in excluded_ids]

    # Build schedule
    console.print(Panel("[bold cyan]Building Schedule[/bold cyan]", expand=False))
    from agent.scheduler import build_schedule
    scheduled = build_schedule(filtered, config)
    keynotes = _load_keynotes()
    schedule_dicts = _scheduled_to_dicts(scheduled) + keynotes
    schedule_dicts.sort(key=lambda x: (x.get("start_date", ""), x.get("scheduled_start") or ""))
    console.print(f"  [green]✓ {len(scheduled)} sessions scheduled across {len(set(ss.day for ss in scheduled))} days[/green]")

    # Step 4: conflict resolver
    schedule_dicts = step_conflict_resolver(schedule_dicts)

    # Step 5: schedule view
    console.print(Panel("[bold cyan]Step 5 — Your Schedule[/bold cyan]", expand=False))
    print_schedule(schedule_dicts)

    # Step 6: export + registration
    results = step_export_and_register(schedule_dicts, config)

    # Step 7: registration status (only if registration was attempted)
    if results:
        step_registration_status(results, schedule_dicts)
    else:
        console.print("\n[bold green]✓ Setup complete![/bold green]")
        console.print(
            "[dim]Next: awsevents register (now) | awsevents watch (auto) | "
            "awsevents schedule | awsevents edit[/dim]"
        )


def _scheduled_to_dicts(scheduled) -> list[dict]:
    out = []
    for ss in scheduled:
        s = ss.session
        out.append({
            "event_id": s.event_id,
            "title": s.title,
            "description": (s.description or "")[:300],
            "start_date": s.start_date.isoformat() if s.start_date else "",
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


def _load_keynotes() -> list[dict]:
    if not _SCHEDULE_PATH.exists():
        return []
    with open(_SCHEDULE_PATH) as f:
        data = json.load(f)
    return [s for s in data if s.get("event_type") == "Keynote"]
