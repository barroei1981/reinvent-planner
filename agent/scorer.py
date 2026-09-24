"""Keyword-based domain and learning-level relevance scorer.

Reads domain configuration from config.yaml and scores each session by
computing the weighted sum of keyword hits across domains, then applies a
learning-level multiplier. Scores are normalized to [0, 1] across all sessions.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent.catalog import Session

# Default level multipliers when not set in config
_DEFAULT_MULTIPLIERS = {
    "preferred": 1.0,
    "acceptable": 0.6,
    "avoid": 0.2,
    "unknown": 0.5,
}


class Scorer:
    def __init__(self, preferences: dict[str, Any]) -> None:
        self._domains: list[dict] = preferences.get("domains", [])
        levels_cfg = preferences.get("levels", {})
        self._preferred: set[str] = set(levels_cfg.get("preferred", []))
        self._acceptable: set[str] = set(levels_cfg.get("acceptable", []))
        self._avoid: set[str] = set(levels_cfg.get("avoid", []))
        mults = preferences.get("level_multipliers", {})
        self._multipliers = {**_DEFAULT_MULTIPLIERS, **mults}

    def _domain_score(self, session: Session) -> float:
        text = f"{session.title} {session.description}".lower()
        total = 0.0
        for domain in self._domains:
            weight = float(domain.get("weight", 1))
            for kw in domain.get("keywords", []):
                if kw.lower() in text:
                    total += weight
        return total

    def _level_multiplier(self, session: Session) -> float:
        lvl = session.learning_level or ""
        if lvl in self._preferred:
            return float(self._multipliers.get("preferred", 1.0))
        if lvl in self._acceptable:
            return float(self._multipliers.get("acceptable", 0.6))
        if lvl in self._avoid:
            return float(self._multipliers.get("avoid", 0.2))
        return float(self._multipliers.get("unknown", 0.5))

    def score_all(self, sessions: list[Session]) -> list[Session]:
        """Return sessions with `.score` set, sorted descending by score."""
        raw: list[tuple[Session, float]] = []
        for s in sessions:
            raw_score = self._domain_score(s) * self._level_multiplier(s)
            raw.append((s, raw_score))

        if not raw:
            return []

        max_score = max(score for _, score in raw) or 1.0
        scored = [replace(s, score=round(raw_score / max_score, 4)) for s, raw_score in raw]
        return sorted(scored, key=lambda s: s.score, reverse=True)
