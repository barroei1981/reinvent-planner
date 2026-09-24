"""Fetch AWS Events catalog directly from the content-directory JSON API.

Mirrors the strategy used by aws-events-mcp but returns our own Session dataclass
so the rest of the agent doesn't depend on that package.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import httpx

_ENDPOINT = "https://aws.amazon.com/api/dirs/items/search"
_DIRECTORY_ID = "alias#events-webinars-interactive-cards"
_TAG_EXCLUSIONS = [
    "!GLOBAL#local-tags-events-master-series#third-party",
    "!GLOBAL#local-tags-series#third-party",
    "!GLOBAL#local-tags-flag#archived",
]
_BASE_PARAMS: dict[str, str] = {
    "item.locale": "en_US",
    "size": "100",
    "sort_by": "item.dateCreated",
    "sort_order": "desc",
}
_MAX_PAGES = 50
_TIMEOUT = 30.0
_USER_AGENT = "awsevents-agent/0.1"

# Maps catalog tag namespaces to values we use
_LOCATION_MODE_TAGS = {
    "in-person": "physical",
    "virtual": "virtual",
    "on-demand": "virtual",
}
_LEVEL_MAP = {
    "foundational": "Foundational",
    "100": "Foundational",
    "intermediate": "Intermediate",
    "200": "Intermediate",
    "advanced": "Advanced",
    "300": "Advanced",
    "expert": "Expert",
    "400": "Expert",
}


@dataclass(frozen=True)
class Session:
    event_id: str
    title: str
    description: str
    start_date: date
    start_time: Optional[str]
    time_zone: Optional[str]
    location: Optional[str]
    location_mode: str          # "physical" | "virtual"
    learning_level: Optional[str]
    event_type: Optional[str]
    partner_name: Optional[str]
    registration_url: Optional[str]
    learn_more_url: Optional[str]

    # Computed at fetch time; not part of equality
    score: float = field(default=0.0, compare=False, hash=False)

    def day_key(self) -> str:
        return self.start_date.isoformat()

    def start_dt(self, default_hour: int = 8) -> datetime:
        """Parse start_time into a datetime for scheduling comparisons."""
        if not self.start_time:
            return datetime.combine(self.start_date, datetime.min.time().replace(hour=default_hour))
        for fmt in ("%I:%M %p", "%H:%M", "%I:%M%p", "%I %p"):
            try:
                t = datetime.strptime(self.start_time.strip().upper(), fmt.upper())
                return datetime.combine(self.start_date, t.time())
            except ValueError:
                continue
        return datetime.combine(self.start_date, datetime.min.time().replace(hour=default_hour))


def _parse_date(raw: Any) -> Optional[date]:
    if not isinstance(raw, str):
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    # Try ISO fragment "2026-12-01T..."
    if "T" in raw:
        try:
            return datetime.fromisoformat(raw.split("T")[0]).date()
        except ValueError:
            pass
    return None


def _derive_location_mode(tags: list[dict]) -> str:
    for tag in tags:
        tag_id = tag.get("id", "") if isinstance(tag, dict) else ""
        for key, mode in _LOCATION_MODE_TAGS.items():
            if key in tag_id.lower():
                return mode
    return "physical"


def _derive_event_type(tags: list[dict]) -> Optional[str]:
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        tag_id = tag.get("id", "")
        if "local-tags-content-type" in tag_id or "aws-event-type" in tag_id:
            label = tag.get("name") or tag.get("description") or ""
            if label:
                return label
    return None


def _normalize_level(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    return _LEVEL_MAP.get(key)


def _parse_record(record: dict) -> Optional[Session]:
    fields = record.get("additionalFields", {})
    if not isinstance(fields, dict):
        fields = {}

    event_id = record.get("id") or fields.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        return None

    title = fields.get("title") or fields.get("heading") or ""
    if not isinstance(title, str) or not title.strip():
        return None

    description = fields.get("body") or fields.get("description") or ""
    if not isinstance(description, str):
        description = ""

    raw_date = fields.get("date") or fields.get("startDate") or fields.get("publishedDate")
    start_date = _parse_date(raw_date)
    if start_date is None:
        return None

    start_time = fields.get("startTime") or fields.get("time")
    if isinstance(start_time, str) and not start_time.strip():
        start_time = None

    time_zone = fields.get("timeZone") or fields.get("timezone")
    if isinstance(time_zone, str) and not time_zone.strip():
        time_zone = None

    location = fields.get("location")
    if isinstance(location, str) and not location.strip():
        location = None

    tags: list[dict] = record.get("tags", [])
    location_mode = record.get("location_mode") or _derive_location_mode(tags)
    event_type = fields.get("eventType") or fields.get("event_type") or _derive_event_type(tags)

    raw_level = fields.get("level") or fields.get("learningLevel")
    learning_level = _normalize_level(raw_level)

    partner_name = fields.get("partnerName") or fields.get("partner")
    if isinstance(partner_name, str) and not partner_name.strip():
        partner_name = None

    reg_url = fields.get("ctaLink") or fields.get("registrationUrl") or fields.get("registration_url")
    learn_url = fields.get("primaryCTALink") or fields.get("learnMoreUrl") or fields.get("learn_more_url")

    if not reg_url and not learn_url:
        learn_url = "https://aws.amazon.com/events/explore-aws-events/"

    return Session(
        event_id=event_id.strip(),
        title=title.strip(),
        description=description.strip(),
        start_date=start_date,
        start_time=start_time,
        time_zone=time_zone,
        location=location,
        location_mode=location_mode if isinstance(location_mode, str) else "physical",
        learning_level=learning_level,
        event_type=event_type if isinstance(event_type, str) else None,
        partner_name=partner_name,
        registration_url=reg_url if isinstance(reg_url, str) and reg_url.strip() else None,
        learn_more_url=learn_url if isinstance(learn_url, str) and learn_url.strip() else None,
    )


async def fetch_sessions(
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    location_text: Optional[str] = None,
    location_mode: Optional[str] = None,
    event_type: Optional[str] = None,
    learning_levels: Optional[list[str]] = None,
) -> list[Session]:
    """Fetch all matching sessions from the AWS Events content-directory API."""
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    sessions: list[Session] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:
        for page in range(_MAX_PAGES):
            params: dict[str, Any] = {**_BASE_PARAMS, "item.directoryId": _DIRECTORY_ID, "page": str(page)}
            params["tags.id"] = _TAG_EXCLUSIONS

            resp = await client.get(_ENDPOINT, params=params)
            resp.raise_for_status()
            payload = resp.json()

            raw_items = payload.get("items", [])
            if not raw_items:
                break

            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                inner = entry.get("item", {})
                if isinstance(inner, dict):
                    inner = dict(inner)
                    if "tags" not in inner:
                        inner["tags"] = entry.get("tags", [])
                else:
                    inner = entry

                session = _parse_record(inner)
                if session is None:
                    continue

                # Apply caller-side filters
                if start_date and session.start_date < start_date:
                    continue
                if end_date and session.start_date > end_date:
                    continue
                if location_mode and session.location_mode != location_mode:
                    continue
                if location_text:
                    loc = (session.location or "").lower()
                    if location_text.lower() not in loc:
                        continue
                if event_type:
                    et = (session.event_type or "").lower()
                    if event_type.lower() not in et:
                        continue
                if learning_levels:
                    lvl = session.learning_level or ""
                    if lvl not in learning_levels:
                        continue

                sessions.append(session)

            total_hits = payload.get("metadata", {}).get("totalHits")
            if total_hits is not None and len(sessions) >= total_hits:
                break

    return sessions
