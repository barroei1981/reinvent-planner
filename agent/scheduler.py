"""Daily schedule optimizer.

Groups scored sessions by day and builds a per-day schedule that:
  1. Maximises total relevance score (greedy, score-descending)
  2. Penalises venue/location changes to minimise physical travel

Algorithm:
  For each day, iterate sessions sorted by (score - hop_penalty) descending.
  A session is accepted if it does not overlap any already-accepted session.
  Overlap = [start, start + duration) windows intersect (with buffer gap).
  hop_penalty = location_change_penalty * hop_cost(prev_venue, candidate_venue)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from agent.catalog import Session


@dataclass
class ScheduledSession:
    session: Session
    day: str           # YYYY-MM-DD
    start: datetime
    end: datetime
    backup: bool = False        # fallback if primary slot registration fails
    backup_note: str = ""


def _resolve_cluster(location: Optional[str], venue_clusters: list[dict]) -> Optional[int]:
    if not location:
        return None
    loc_lower = location.lower()
    for idx, cluster in enumerate(venue_clusters):
        for kw in cluster.get("keywords", []):
            if kw.lower() in loc_lower:
                return idx
    return None


def _hop_cost(
    cluster_a: Optional[int],
    cluster_b: Optional[int],
    hop_costs: list[list[int]],
) -> int:
    if cluster_a is None or cluster_b is None:
        return 0
    if cluster_a == cluster_b:
        return 0
    try:
        return hop_costs[cluster_a][cluster_b]
    except (IndexError, TypeError):
        return 2


def _duration_minutes(session: Session, default_durations: dict[str, int]) -> int:
    et = session.event_type or "default"
    for key, mins in default_durations.items():
        if key.lower() in et.lower():
            return mins
    return default_durations.get("default", 60)


def _overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def _classify_domain(session: Session, domains: list[dict]) -> str:
    """Return the domain name with the most keyword matches (case-insensitive)."""
    text = f"{session.title} {session.description or ''}".lower()
    best_name, best_hits = "Other", 0
    for d in domains:
        hits = sum(1 for kw in d.get("keywords", []) if kw.lower() in text)
        if hits > best_hits:
            best_hits = hits
            best_name = d["name"]
    return best_name


def build_schedule(
    sessions: list[Session],
    config: dict[str, Any],
) -> list[ScheduledSession]:
    """Return a per-day schedule from pre-scored sessions."""
    prefs = config.get("preferences", {})
    venue_clusters_cfg: list[dict] = list(config.get("venue_clusters", {}).values())
    hop_costs: list[list[int]] = config.get("venue_hop_costs", [[0]])
    max_per_day: int = prefs.get("max_sessions_per_day", 6)
    buffer_mins: int = prefs.get("buffer_minutes", 15)
    hop_penalty: float = float(prefs.get("location_change_penalty", 3.0))
    default_durations: dict[str, int] = prefs.get("default_duration_minutes", {"default": 60})
    if isinstance(default_durations, dict) and "default" not in default_durations:
        default_durations["default"] = 60

    # Domain balance config
    db = prefs.get("domain_balance", {})
    balance_enabled: bool = bool(db.get("enabled", False))
    first_bonus: float = float(db.get("first_session_bonus", 0.0))
    second_bonus: float = float(db.get("second_session_bonus", 0.0))
    max_domain_per_day: int = int(db.get("max_per_day", 999))
    cap_penalty: float = float(db.get("cap_penalty", 0.0))
    domains_cfg: list[dict] = prefs.get("domains", []) if balance_enabled else []

    # Group by day, each day sorted by raw score desc
    by_day: dict[str, list[Session]] = {}
    for s in sessions:
        by_day.setdefault(s.day_key(), []).append(s)
    for day in by_day:
        by_day[day].sort(key=lambda s: s.score, reverse=True)

    schedule: list[ScheduledSession] = []

    for day in sorted(by_day):
        candidates = by_day[day]
        accepted: list[ScheduledSession] = []
        last_cluster: Optional[int] = None
        domain_counts: dict[str, int] = {}

        remaining = list(candidates)

        while len(accepted) < max_per_day and remaining:
            best_idx: Optional[int] = None
            best_adjusted: float = -1.0

            for idx, cand in enumerate(remaining):
                start = cand.start_dt()
                dur = _duration_minutes(cand, default_durations)
                end = start + timedelta(minutes=dur) + timedelta(minutes=buffer_mins)

                # Check for time overlap with accepted sessions
                conflict = any(
                    _overlaps(start, start + timedelta(minutes=dur), a.start, a.end - timedelta(minutes=buffer_mins))
                    for a in accepted
                )
                if conflict:
                    continue

                cluster = _resolve_cluster(cand.location, venue_clusters_cfg)
                cost = _hop_cost(last_cluster, cluster, hop_costs)
                adjusted = cand.score - hop_penalty * cost * 0.1

                # Domain diversity adjustment
                if domains_cfg:
                    domain = _classify_domain(cand, domains_cfg)
                    count = domain_counts.get(domain, 0)
                    if count == 0:
                        adjusted += first_bonus
                    elif count == 1:
                        adjusted += second_bonus
                    if count >= max_domain_per_day:
                        adjusted -= cap_penalty

                if adjusted > best_adjusted:
                    best_adjusted = adjusted
                    best_idx = idx

            if best_idx is None:
                break

            chosen = remaining.pop(best_idx)
            start = chosen.start_dt()
            dur = _duration_minutes(chosen, default_durations)
            end = start + timedelta(minutes=dur)
            ss = ScheduledSession(session=chosen, day=day, start=start, end=end)
            accepted.append(ss)
            last_cluster = _resolve_cluster(chosen.location, venue_clusters_cfg)
            if domains_cfg:
                dom = _classify_domain(chosen, domains_cfg)
                domain_counts[dom] = domain_counts.get(dom, 0) + 1
            schedule.append(ss)

        # Sort day's accepted sessions by start time for display
        accepted.sort(key=lambda s: s.start)

    return sorted(schedule, key=lambda s: (s.day, s.start))
