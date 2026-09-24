"""Keyword-based domain and learning-level relevance scorer.

Handles two session types:
  - catalog.Session   (from the public AWS Events API)
  - wishlist.ReinventSession  (scraped from registration.awsevents.com)

Wishlist items are always scored 1.0 (locked favorites).
Non-wishlist sessions are scored by keyword domain match × level multiplier,
then normalised to [0, 1].
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Union

from agent.catalog import Session

# Default level multipliers when not set in config
_DEFAULT_MULTIPLIERS = {
    "preferred": 1.0,
    "acceptable": 0.6,
    "avoid": 0.2,
    "unknown": 0.5,
}

# re:Invent official topic track names → keyword groups (used when the full
# catalog is available; the session's `topics` list carries these track labels)
_REINVENT_TRACKS: dict[str, list[str]] = {
    "AI/ML": ["artificial intelligence", "machine learning", "deep learning", "sagemaker", "bedrock", "generative ai", "llm", "foundation model", "ai agent", "rag"],
    "Analytics": ["analytics", "redshift", "athena", "glue", "kinesis", "quicksight", "data lake", "streaming", "etl", "lake formation"],
    "Application Integration": ["eventbridge", "step functions", "sns", "sqs", "api gateway", "mq", "appflow"],
    "Compute": ["ec2", "graviton", "nitro", "auto scaling", "spot", "batch", "hpc", "compute optimizer"],
    "Containers": ["eks", "ecs", "fargate", "container", "kubernetes", "docker", "ecr", "app runner"],
    "Database": ["rds", "aurora", "dynamodb", "elasticache", "documentdb", "neptune", "timestream", "memorydb"],
    "Developer Tools": ["codewhisperer", "codeguru", "devops", "codecatalyst", "ci/cd", "codepipeline", "copilot", "sdk"],
    "Management & Governance": ["cloudwatch", "cloudtrail", "config", "organizations", "control tower", "systems manager", "cost"],
    "Migration": ["migration", "dms", "transfer", "mgt", "cloud migration", "modernization", "lift and shift"],
    "Networking & Content Delivery": ["vpc", "cloudfront", "route 53", "direct connect", "transit gateway", "load balancer", "network firewall"],
    "Security": ["security", "iam", "guardduty", "inspector", "macie", "shield", "waf", "detective", "zero trust", "compliance", "encryption"],
    "Serverless": ["lambda", "serverless", "api gateway", "step functions", "fargate", "sam", "eventbridge"],
    "Storage": ["s3", "ebs", "efs", "fsx", "backup", "glacier", "storage gateway"],
}


def _level_from_str(raw: str | None) -> str:
    if not raw:
        return ""
    r = raw.strip().lower()
    if "foundational" in r or "100" in r:
        return "Foundational"
    if "intermediate" in r or "200" in r:
        return "Intermediate"
    if "advanced" in r or "300" in r:
        return "Advanced"
    if "expert" in r or "400" in r:
        return "Expert"
    return raw.strip()


class Scorer:
    def __init__(self, preferences: dict[str, Any]) -> None:
        self._domains: list[dict] = preferences.get("domains", [])
        levels_cfg = preferences.get("levels", {})
        self._preferred: set[str] = set(levels_cfg.get("preferred", []))
        self._acceptable: set[str] = set(levels_cfg.get("acceptable", []))
        self._avoid: set[str] = set(levels_cfg.get("avoid", []))
        mults = preferences.get("level_multipliers", {})
        self._multipliers = {**_DEFAULT_MULTIPLIERS, **mults}

        # re:Invent topic track preferences (optional; extends domain keywords)
        self._topic_tracks: set[str] = set(preferences.get("topic_tracks", []))
        self._session_types: set[str] = set(t.lower() for t in preferences.get("session_types", []))

    def _domain_score_text(self, text: str) -> float:
        total = 0.0
        for domain in self._domains:
            weight = float(domain.get("weight", 1))
            for kw in domain.get("keywords", []):
                if kw.lower() in text:
                    total += weight
        return total

    def _track_score(self, topics: list[str]) -> float:
        """Bonus for sessions whose topic track matches user preferences."""
        if not self._topic_tracks or not topics:
            return 0.0
        score = 0.0
        for t in topics:
            if t in self._topic_tracks:
                score += 5.0
            # partial match against known track keyword groups
            for track_name, kws in _REINVENT_TRACKS.items():
                if track_name in self._topic_tracks:
                    if any(kw in t.lower() for kw in kws):
                        score += 3.0
        return score

    def _type_multiplier(self, session_type: str | None) -> float:
        """Multiplier based on preferred session formats."""
        if not self._session_types or not session_type:
            return 1.0
        if session_type.lower() in self._session_types:
            return 1.0
        return 0.7  # mild penalty for non-preferred type

    def _level_multiplier(self, level: str | None) -> float:
        lvl = level or ""
        if lvl in self._preferred:
            return float(self._multipliers.get("preferred", 1.0))
        if lvl in self._acceptable:
            return float(self._multipliers.get("acceptable", 0.6))
        if lvl in self._avoid:
            return float(self._multipliers.get("avoid", 0.2))
        return float(self._multipliers.get("unknown", 0.5))

    # ── Public API ────────────────────────────────────────────────────────────

    def score_all(self, sessions: list[Session]) -> list[Session]:
        """Score public-catalog Sessions. Normalised to [0, 1]."""
        raw: list[tuple[Session, float]] = []
        for s in sessions:
            text = f"{s.title} {s.description}".lower()
            domain = self._domain_score_text(text)
            level_m = self._level_multiplier(s.learning_level)
            raw_score = domain * level_m
            raw.append((s, raw_score))

        if not raw:
            return []
        max_score = max(v for _, v in raw) or 1.0
        scored = [replace(s, score=round(v / max_score, 4)) for s, v in raw]
        return sorted(scored, key=lambda s: s.score, reverse=True)

    def score_reinvent_sessions(self, sessions: list) -> list[Session]:
        """Score ReinventSession objects (from wishlist.py) into catalog.Session format.

        Wishlisted sessions get a fixed score of 1.0.
        Others are scored by domain keywords + topic track + session type + level.
        Returns list[Session] sorted descending by score.
        """
        from agent.wishlist import ReinventSession

        result: list[tuple[Session, float]] = []

        for rs in sessions:
            if not isinstance(rs, ReinventSession):
                continue

            # Convert to catalog.Session
            from datetime import date as dt_date
            try:
                start_date = dt_date.fromisoformat(rs.date) if rs.date else dt_date.today()
            except (ValueError, TypeError):
                start_date = dt_date.today()

            level = _level_from_str(rs.level)

            from agent.catalog import Session as CSession
            s = CSession(
                event_id=rs.session_id or rs.title[:40],
                title=rs.title,
                description=rs.description,
                start_date=start_date,
                start_time=rs.time,
                time_zone=None,
                location=f"{rs.venue or ''} {rs.room or ''}".strip() or None,
                location_mode="physical",
                learning_level=level or None,
                event_type=rs.session_type,
                partner_name=None,
                registration_url=rs.registration_url,
                learn_more_url=rs.registration_url,
            )

            if rs.is_wishlisted:
                # Locked priority — score set after normalisation
                result.append((s, float("inf")))
                continue

            text = f"{rs.title} {rs.description} {' '.join(rs.topics)}".lower()
            domain = self._domain_score_text(text)
            track = self._track_score(rs.topics)
            type_m = self._type_multiplier(rs.session_type)
            level_m = self._level_multiplier(level)
            raw_score = (domain + track) * type_m * level_m
            result.append((s, raw_score))

        if not result:
            return []

        # Normalise non-wishlist scores to [0, 0.99]; wishlist always = 1.0
        finite_scores = [v for _, v in result if v != float("inf")]
        max_finite = max(finite_scores) if finite_scores else 1.0

        final: list[Session] = []
        for s, v in result:
            if v == float("inf"):
                final.append(replace(s, score=1.0))
            else:
                norm = round((v / max_finite) * 0.99, 4) if max_finite > 0 else 0.0
                final.append(replace(s, score=norm))

        return sorted(final, key=lambda s: s.score, reverse=True)
