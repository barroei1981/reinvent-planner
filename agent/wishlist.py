"""Fetch the re:Invent session catalog and user wishlist.

Primary flow (no browser needed):
  1. Read auth cookies from Chrome Profile 1 via browser_cookie3.
  2. Call catalog.awsevents.com/api/sessions with Rainfocus API headers.
  3. Paginate through all sessions (50 per page).
  4. Call catalog.awsevents.com/api/myData for wishlisted session IDs.
  5. Save full catalog to reinvent_catalog.json and wishlist to wishlist.json.

Playwright fallback (used only when API fails):
  Opens a visible Chrome window and scrapes the registration catalog page.

The saved files are consumed by the scorer (wishlist items get a hard score of 1.0
and are locked into the schedule regardless of keyword score) and by the registrar
(wishlist items are registered first).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, replace as dc_replace
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import async_playwright, Page, BrowserContext, Locator
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

try:
    import httpx as _httpx
    import browser_cookie3 as _bc3
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False

# re:Invent 2026 catalog URL (Playwright fallback)
_CATALOG_URL = "https://registration.awsevents.com/flow/awsevents/reinvent2026/event-catalog/page/eventCatalog"

# catalog.awsevents.com REST API (Rainfocus)
_CATALOG_API_BASE = "https://catalog.awsevents.com"
_RF_PROFILE_ID = "jt8iJfCKNifsa0l2GD8VeKJXwDkZ9mHD"
_RF_WIDGET_ID = "yloMDinvijFk6PtNtWambWamU6mPKiRf"
_RF_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://registration.awsevents.com/",
    "rfapiprofileid": _RF_PROFILE_ID,
    "rfwidgetid": _RF_WIDGET_ID,
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
}
_CHROME_PROFILE_1_COOKIES = (
    Path.home() / "Library/Application Support/Google/Chrome/Profile 1/Cookies"
)
_LOGIN_URLS = [
    "https://registration.awsevents.com/",
    "https://builder.aws.com",
]

_WISHLIST_PATH = Path("wishlist.json")
_CATALOG_PATH = Path("reinvent_catalog.json")


@dataclass
class ReinventSession:
    session_id: str
    title: str
    description: str
    date: Optional[str]
    time: Optional[str]
    venue: Optional[str]
    room: Optional[str]
    level: Optional[str]
    session_type: Optional[str]
    topics: list[str]
    areas_of_interest: list[str]
    roles: list[str]
    speakers: list[str]
    is_wishlisted: bool
    capacity: Optional[str]
    registration_url: Optional[str]


async def _wait_for_login(page: Page, email: str, password: str) -> bool:
    """Handle the AWS Builder ID login flow. Returns True when logged in.

    Attempts automated credential filling; if that fails (ERR-837, corporate SSO,
    CAPTCHA), prints instructions and waits up to 5 minutes for the user to
    complete login manually in the visible browser window.
    """
    await page.wait_for_load_state("networkidle")

    # ── Step 1: email ─────────────────────────────────────────────────────
    email_input = page.locator("input[type='email'], input[placeholder*='username' i], input[placeholder*='email' i]").first
    try:
        await email_input.wait_for(state="visible", timeout=8000)
        await email_input.fill(email)
        continue_btn = page.locator("button:has-text('Continue'), input[type='submit']").first
        await continue_btn.click()
        await page.wait_for_load_state("networkidle")
        print("[wishlist] Email submitted")
    except Exception as exc:
        print(f"[wishlist] WARNING: email step failed: {exc}")

    # ── Step 2: password ──────────────────────────────────────────────────
    pw_input = page.locator("input[type='password']").first
    try:
        await pw_input.wait_for(state="visible", timeout=8000)
        await pw_input.fill(password)
        sign_in_btn = page.locator(
            "button:has-text('Sign in'), button:has-text('Continue'), input[type='submit']"
        ).first
        await sign_in_btn.click()
        await page.wait_for_load_state("networkidle")
        print("[wishlist] Password submitted")
    except Exception as exc:
        print(f"[wishlist] WARNING: password step failed: {exc}")

    # ── Step 3: MFA / TOTP ────────────────────────────────────────────────
    try:
        mfa_input = page.locator(
            "input[name='mfaCode'], input[autocomplete='one-time-code'], "
            "input[placeholder*='code' i], input[placeholder*='MFA' i]"
        ).first
        if await mfa_input.is_visible(timeout=4000):
            print("[wishlist] ⚠  MFA required — enter your code in the browser window")
    except Exception:
        pass

    # ── Step 4: post-login consent / terms ───────────────────────────────
    for btn_text in ["Accept", "I Accept", "I Agree", "Agree", "Continue", "OK"]:
        try:
            btn = page.locator(f"button:has-text('{btn_text}')").first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_load_state("networkidle")
        except Exception:
            continue

    # ── Step 5: wait for catalog URL — prompt for manual completion ───────
    catalog_pattern = re.compile(r"(reinvent2026|eventCatalog|event-catalog|dashboard|myagenda)", re.I)

    # Quick check — did automated login succeed?
    try:
        await page.wait_for_url(catalog_pattern, timeout=8000)
        return True
    except Exception:
        pass

    # Automated login likely failed (ERR-837, corporate SSO, CAPTCHA).
    # Give the user time to complete login manually in the browser window.
    current = page.url.lower()
    if "signin.aws" in current or "login" in current or "sso" in current:
        print(
            "\n[wishlist] ─────────────────────────────────────────────────────\n"
            "[wishlist] Automated login could not complete — a browser window\n"
            "[wishlist] should be open. Please log in manually, then the script\n"
            "[wishlist] will continue automatically (waiting up to 5 minutes).\n"
            "[wishlist] ─────────────────────────────────────────────────────"
        )
        try:
            await page.wait_for_url(catalog_pattern, timeout=300_000)
            return True
        except Exception:
            pass

    current_url = page.url.lower()
    return "signin.aws" not in current_url and "login" not in current_url


async def _scrape_sessions_from_page(page: Page) -> list[dict]:
    """Extract session cards from the re:Invent catalog page via JavaScript.

    Handles both the Rainfocus registration.awsevents.com DOM structure and
    fallback generic selectors. Captures catalog facets: topic, area_of_interest,
    role, features (format), level, date, time, venue, room.
    """
    return await page.evaluate("""
        () => {
            const sessions = [];

            // Rainfocus / re:Invent uses these card selectors
            const CARD_SELECTORS = [
                // Rainfocus registration workflow cards
                '.rf-event-item', '.rf-session-item', '[class*="rfEvent"]',
                // re:Invent specific
                '[class*="session-card"]', '[class*="sessionCard"]',
                // Cvent fallbacks
                '[data-testid="session-card"]', '.evy-session-card',
                '.fd-session', '[data-component="session"]',
            ];

            let cards = [];
            for (const sel of CARD_SELECTORS) {
                const found = document.querySelectorAll(sel);
                if (found.length > 0) { cards = Array.from(found); break; }
            }

            // Last-resort: any li/div that contains session-like content
            if (!cards.length) {
                cards = Array.from(document.querySelectorAll(
                    'li[class*="item"], article, [role="listitem"]'
                )).filter(el => el.querySelector('h2, h3, h4'));
            }

            const getAll = (el, sel) =>
                Array.from(el.querySelectorAll(sel)).map(e => e.textContent.trim()).filter(Boolean);

            const getFirst = (el, sel) => {
                const e = el.querySelector(sel); return e ? e.textContent.trim() : null;
            };

            cards.forEach(card => {
                // ── Wishlist indicator ───────────────────────────────────────
                const wishlisted = !!(
                    card.querySelector('[aria-label*="Remove from" i], [aria-label*="Added to" i], [class*="bookmarked" i], [class*="wishlisted" i], [class*="saved" i]') ||
                    card.classList.contains('saved') || card.classList.contains('bookmarked') ||
                    card.getAttribute('data-saved') === 'true' || card.getAttribute('data-bookmarked') === 'true'
                );

                const sessionId = card.getAttribute('data-id') || card.getAttribute('data-session-id') ||
                                  card.getAttribute('data-event-id') || card.id || null;

                const title = getFirst(card, 'h2, h3, h4, [class*="title"], [class*="name"]');
                const desc  = getFirst(card, '[class*="description"], [class*="abstract"], [class*="body"], p');

                // ── re:Invent catalog facets ─────────────────────────────────
                // The catalog shows labeled facets; try to extract by label text
                const facets = {};
                const labelEls = card.querySelectorAll('[class*="label"], [class*="tag-label"], [class*="facet"], dt, th');
                labelEls.forEach(lbl => {
                    const key = lbl.textContent.trim().toLowerCase().replace(/[^a-z]+/g, '_');
                    const val = lbl.nextElementSibling ? lbl.nextElementSibling.textContent.trim() : null;
                    if (key && val) facets[key] = val;
                });

                // Collect all tag/pill text nodes and map to facet categories
                const allTags = getAll(card, '[class*="tag"], [class*="pill"], [class*="badge"], [class*="chip"]');

                // Level — look for the specific "level" labeled area first
                const level = getFirst(card, '[class*="level"], [class*="audience"], [aria-label*="level" i]') ||
                              facets['level'] || facets['audience_level'] || null;

                // Session type / format / features
                const sessionType = getFirst(card,
                    '[class*="type"], [class*="format"], [class*="session-type"], [class*="sessionType"], [class*="feature"]'
                ) || facets['type'] || facets['format'] || facets['features'] || null;

                // Location
                const venue = getFirst(card, '[class*="venue"], [class*="building"], [class*="location"]') ||
                              facets['venue'] || facets['location'] || null;
                const room  = getFirst(card, '[class*="room"]') || facets['room'] || null;

                // Date / time — re:Invent shows "Monday, Nov 30 · 4:30 PM – 5:30 PM PST"
                const datetime = getFirst(card, '[class*="date"], [class*="time"], [class*="schedule"], time') ||
                                 facets['date'] || facets['time'] || null;
                let date = null, time = null;
                if (datetime) {
                    const m = datetime.match(/([A-Za-z]+,?\\s+[A-Za-z]+\\s+\\d+)/);
                    if (m) date = m[1];
                    const t = datetime.match(/(\\d{1,2}:\\d{2}\\s*(?:AM|PM))/i);
                    if (t) time = t[1];
                }

                // Topic (re:Invent facet: "Topic")
                const topicEl = getFirst(card, '[class*="topic"]');
                const topics = topicEl
                    ? topicEl.split(',').map(s => s.trim()).filter(Boolean)
                    : getAll(card, '[class*="topic"]');

                // Area of Interest (re:Invent facet)
                const aoiEl = getFirst(card, '[class*="interest"], [class*="area"], [class*="aoi"]') ||
                              facets['area_of_interest'] || null;
                const areas_of_interest = aoiEl
                    ? aoiEl.split(',').map(s => s.trim()).filter(Boolean)
                    : [];

                // Role
                const roleEl = getFirst(card, '[class*="role"], [class*="audience"]') || facets['role'] || null;
                const roles = roleEl
                    ? roleEl.split(',').map(s => s.trim()).filter(Boolean)
                    : [];

                // Speakers
                const speakers = getAll(card, '[class*="speaker"], [class*="presenter"]');

                // Link
                const linkEl = card.querySelector('a[href]');
                const link = linkEl ? linkEl.getAttribute('href') : null;

                if (title) {
                    sessions.push({
                        session_id: sessionId,
                        title,
                        description: desc || '',
                        date,
                        time,
                        venue,
                        room,
                        level,
                        session_type: sessionType,
                        topics,
                        areas_of_interest,
                        roles,
                        speakers,
                        all_tags: allTags,
                        is_wishlisted: wishlisted,
                        capacity: null,
                        registration_url: link,
                    });
                }
            });
            return sessions;
        }
    """)


async def _scrape_via_network_intercept(page: Page) -> list[dict]:
    """Intercept Cvent API responses to get structured session data.

    Cvent's catalog is backed by XHR/fetch calls that return JSON; intercepting
    these gives us cleaner data than DOM scraping.
    """
    captured: list[dict] = []

    async def handle_response(response):
        url = response.url.lower()
        # Cvent session search / catalog endpoints
        if any(kw in url for kw in ["session", "catalog", "search", "agenda"]):
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    body = await response.json()
                    # Cvent wraps data in various shapes; handle common ones
                    items = None
                    if isinstance(body, list):
                        items = body
                    elif isinstance(body, dict):
                        for key in ("sessions", "items", "data", "results", "hits"):
                            if isinstance(body.get(key), list):
                                items = body[key]
                                break
                    if items:
                        captured.extend(items)
            except Exception:
                pass

    page.on("response", handle_response)
    # Trigger a reload to capture the initial data load
    await page.reload(wait_until="networkidle")
    # Let async handlers fire
    await asyncio.sleep(2)
    page.remove_listener("response", handle_response)
    return captured


async def _get_total_pages(page: Page) -> int:
    """Try to read the total page count from pagination controls."""
    try:
        result = await page.evaluate("""
            () => {
                const pager = document.querySelector('[data-testid="pagination"], .pagination, [class*="paginat"]');
                if (!pager) return 1;
                const nums = Array.from(pager.querySelectorAll('button, a, span'))
                    .map(el => parseInt(el.textContent.trim(), 10))
                    .filter(n => !isNaN(n));
                return nums.length ? Math.max(...nums) : 1;
            }
        """)
        return max(1, int(result))
    except Exception:
        return 1


async def _click_next_page(page: Page) -> bool:
    """Click the 'next' pagination button. Returns False when no next page."""
    for selector in [
        "button[aria-label='Next page']",
        "button[aria-label='Next']",
        "[data-testid='pagination-next']",
        ".pagination-next",
        "button:has-text('Next')",
        "li.next > a",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1500) and await btn.is_enabled(timeout=1500):
                await btn.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(0.8)
                return True
        except Exception:
            continue
    return False


def _load_chrome_profile1_cookies() -> tuple[dict[str, str], str]:
    """Extract awsevents cookies from Chrome Profile 1 via browser_cookie3.

    Returns (cookies_dict, rfjwt_token).
    Loads in priority order: catalog.awsevents.com JSESSIONID takes precedence.
    """
    if not _API_AVAILABLE:
        return {}, ""
    if not _CHROME_PROFILE_1_COOKIES.exists():
        return {}, ""
    cookies: dict[str, str] = {}
    rfjwt = ""
    # Load lower-priority first; higher-priority domains overwrite duplicates
    for domain in ["registration.awsevents.com", ".awsevents.com", "catalog.awsevents.com"]:
        try:
            for c in _bc3.chrome(cookie_file=str(_CHROME_PROFILE_1_COOKIES), domain_name=domain):
                cookies[c.name] = c.value
                if c.name == "rfjwt":
                    rfjwt = c.value
        except Exception:
            pass
    return cookies, rfjwt


def _normalise_api_item(item: dict) -> Optional[ReinventSession]:
    """Map a catalog.awsevents.com API item to ReinventSession."""
    title = (item.get("title") or "").strip()
    if not title:
        return None

    # Parse attributevalues into {attribute_id: [value, ...]}
    attrs: dict[str, list[str]] = {}
    for av in item.get("attributevalues", []):
        attr_id = av.get("attribute_id", "")
        value = (av.get("value") or "").strip()
        if attr_id and value:
            attrs.setdefault(attr_id, []).append(value)

    topics = attrs.get("Topic", [])
    areas_of_interest = attrs.get("AreaofInterest", [])
    roles = attrs.get("Role", [])

    # Level: API returns "200 – Intermediate" or "300 – Advanced"
    level_raw = (attrs.get("Level") or [""])[0]
    level: Optional[str] = None
    if "–" in level_raw:
        level = level_raw.split("–")[-1].strip()
    elif level_raw:
        level = level_raw.strip()

    # Session type: prefer the "Type" attributevalue; fall back to item.type
    session_type: Optional[str] = (attrs.get("Type") or [item.get("type") or ""])[0] or None

    # Schedule from the first time slot
    times = item.get("times", [])
    date: Optional[str] = None
    time_str: Optional[str] = None
    room: Optional[str] = None
    capacity: Optional[str] = None
    end_time: Optional[str] = None
    if times:
        t = times[0]
        date = t.get("date")
        time_str = t.get("startTime") or t.get("time")
        end_time = t.get("endTime")
        room = t.get("room")
        cap = t.get("capacity")
        capacity = str(cap) if cap is not None else None

    # Venue: prefer dedicated Venue attributevalue, then parse from room
    venue_list = attrs.get("Venue", [])
    venue: Optional[str] = venue_list[0] if venue_list else None
    if not venue and room:
        venue = room.split("|")[0].strip()

    # Speakers from participants
    speakers = [
        (p.get("fullName") or p.get("globalFullName") or "").strip()
        for p in item.get("participants", [])
        if (p.get("fullName") or p.get("globalFullName") or "").strip()
    ]

    session_id = item.get("sessionID") or item.get("externalID") or ""
    description = (item.get("abstract") or "").strip()
    reg_url = (
        f"https://registration.awsevents.com/flow/awsevents/reinvent2026/"
        f"event-catalog/page/eventCatalog?session={session_id}"
        if session_id else None
    )

    return ReinventSession(
        session_id=session_id,
        title=title,
        description=description,
        date=date,
        time=time_str,
        venue=venue,
        room=room,
        level=level,
        session_type=session_type,
        topics=topics,
        areas_of_interest=areas_of_interest,
        roles=roles,
        speakers=speakers,
        is_wishlisted=False,
        capacity=capacity,
        registration_url=reg_url,
    )


async def _fetch_catalog_via_api() -> tuple[list[ReinventSession], list[ReinventSession]]:
    """Fetch all re:Invent sessions from the catalog.awsevents.com REST API.

    Uses browser_cookie3 to read auth cookies from Chrome Profile 1.
    Sends rfAuthToken header (from rfjwt cookie) to enable full pagination.
    No browser window is opened.
    Returns (all_sessions, wishlisted_sessions).
    """
    if not _API_AVAILABLE:
        raise RuntimeError("httpx or browser_cookie3 not installed — run: uv add httpx browser-cookie3")

    from urllib.parse import urlencode

    PAGE_SIZE = 50
    cookies, rfjwt = _load_chrome_profile1_cookies()
    print(f"[catalog] Loaded {len(cookies)} cookies from Chrome Profile 1")

    auth_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://registration.awsevents.com",
        "Referer": (
            "https://registration.awsevents.com/flow/awsevents/reinvent2026/"
            "event-catalog/page/eventCatalog"
        ),
        "rfApiProfileId": _RF_PROFILE_ID,
        "rfWidgetId": _RF_WIDGET_ID,
    }
    if rfjwt:
        auth_headers["rfAuthToken"] = rfjwt

    def _items_from_response(d: dict) -> tuple[list, int]:
        """Extract (items, total) from either response shape.

        Unauthenticated (from=0): sectionList[0].items + totalSearchItems
        Authenticated pagination: top-level items + total
        """
        # Authenticated pagination shape
        items = d.get("items", [])
        total = d.get("total") or d.get("totalSearchItems", 0)
        if items:
            return items, int(total)
        # First-page shape (sectionList)
        sl = d.get("sectionList", [])
        if sl:
            items = sl[0].get("items", [])
            total = d.get("totalSearchItems", 0)
            return items, int(total)
        return [], 0

    all_sessions: list[ReinventSession] = []
    wishlisted_ids: set[str] = set()

    async with _httpx.AsyncClient(cookies=cookies, headers=auth_headers, timeout=30) as client:
        # Probe first page to get total
        r0 = await client.post(
            f"{_CATALOG_API_BASE}/api/sessions",
            content=urlencode({"from": 0, "size": PAGE_SIZE}),
        )
        r0.raise_for_status()
        first_items, total = _items_from_response(r0.json())
        if not total:
            raise RuntimeError(
                "catalog.awsevents.com returned 0 sessions — catalog may not be open yet"
            )
        print(f"[catalog] Total sessions: {total}")
        for item in first_items:
            s = _normalise_api_item(item)
            if s:
                all_sessions.append(s)

        # Fetch remaining pages
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        for page_num in range(1, pages):
            offset = page_num * PAGE_SIZE
            if page_num % 5 == 0:
                print(f"[catalog] Page {page_num + 1}/{pages}...")
            r = await client.post(
                f"{_CATALOG_API_BASE}/api/sessions",
                content=urlencode({"from": offset, "size": PAGE_SIZE}),
            )
            r.raise_for_status()
            items, _ = _items_from_response(r.json())
            for item in items:
                s = _normalise_api_item(item)
                if s:
                    all_sessions.append(s)

        # Wishlist via myData
        r2 = await client.post(f"{_CATALOG_API_BASE}/api/myData")
        if r2.status_code == 200:
            d2 = r2.json()
            if "sessionInterests" in d2:
                for si in d2.get("sessionInterests", []):
                    sid = si.get("sessionID") or si.get("sessionId") or ""
                    if sid:
                        wishlisted_ids.add(sid)
                print(f"[catalog] Wishlist: {len(wishlisted_ids)} bookmarked sessions")
            elif d2.get("responseCode") == "107":
                print("[catalog] Wishlist: not authenticated — skipping")

    # Mark wishlisted sessions
    if wishlisted_ids:
        all_sessions = [
            dc_replace(s, is_wishlisted=True) if s.session_id in wishlisted_ids else s
            for s in all_sessions
        ]

    wishlisted = [s for s in all_sessions if s.is_wishlisted]
    print(f"[catalog] Done: {len(all_sessions)} sessions, {len(wishlisted)} wishlisted")
    return all_sessions, wishlisted


def _normalise_raw(raw: dict) -> Optional[ReinventSession]:
    """Map a raw scraped dict (DOM or API) to ReinventSession."""
    title = raw.get("title") or raw.get("name") or raw.get("sessionName") or raw.get("sessionTitle")
    if not title or not isinstance(title, str) or not title.strip():
        return None

    sid = (raw.get("session_id") or raw.get("id") or raw.get("sessionId") or raw.get("eventId") or "")
    if not isinstance(sid, str):
        sid = str(sid)

    level_raw = raw.get("level") or raw.get("audienceLevel") or raw.get("sessionLevel") or ""
    level = level_raw.strip() if isinstance(level_raw, str) else None

    topics_raw = raw.get("topics") or raw.get("interests") or raw.get("tags") or []
    if isinstance(topics_raw, list):
        topics = [str(t).strip() for t in topics_raw if t]
    elif isinstance(topics_raw, str):
        topics = [topics_raw.strip()]
    else:
        topics = []

    speakers_raw = raw.get("speakers") or raw.get("presenters") or []
    if isinstance(speakers_raw, list):
        speakers = [
            (s.get("name") or s.get("fullName") or str(s)).strip()
            if isinstance(s, dict) else str(s).strip()
            for s in speakers_raw
        ]
    else:
        speakers = []

    wishlisted = bool(raw.get("is_wishlisted") or raw.get("bookmarked") or raw.get("saved") or raw.get("inAgenda"))

    reg_url = raw.get("registration_url") or raw.get("registrationUrl") or raw.get("url") or raw.get("link")
    if isinstance(reg_url, str) and reg_url and not reg_url.startswith("http"):
        reg_url = "https://registration.awsevents.com" + reg_url

    # Areas of interest (re:Invent catalog facet)
    aoi_raw = raw.get("areas_of_interest") or raw.get("areaOfInterest") or raw.get("interests") or []
    if isinstance(aoi_raw, list):
        areas_of_interest = [str(a).strip() for a in aoi_raw if a]
    elif isinstance(aoi_raw, str):
        areas_of_interest = [aoi_raw.strip()]
    else:
        areas_of_interest = []

    # Roles (re:Invent catalog facet)
    roles_raw = raw.get("roles") or raw.get("role") or raw.get("targetAudience") or []
    if isinstance(roles_raw, list):
        roles = [str(r).strip() for r in roles_raw if r]
    elif isinstance(roles_raw, str):
        roles = [roles_raw.strip()]
    else:
        roles = []

    return ReinventSession(
        session_id=sid.strip(),
        title=title.strip(),
        description=(raw.get("description") or raw.get("body") or raw.get("abstract") or "").strip(),
        date=raw.get("date") or raw.get("startDate") or raw.get("sessionDate"),
        time=raw.get("time") or raw.get("startTime") or raw.get("sessionTime"),
        venue=raw.get("venue") or raw.get("building") or raw.get("location") or raw.get("venueName"),
        room=raw.get("room") or raw.get("roomName"),
        level=level or None,
        session_type=raw.get("session_type") or raw.get("sessionType") or raw.get("format"),
        topics=topics,
        areas_of_interest=areas_of_interest,
        roles=roles,
        speakers=speakers,
        is_wishlisted=wishlisted,
        capacity=raw.get("capacity") or raw.get("seatsAvailable"),
        registration_url=reg_url if isinstance(reg_url, str) else None,
    )


_CHROME_USER_DATA_DIR = Path.home() / "Library/Application Support/Google/Chrome"


async def sync_wishlist(email: str, password: str, *, headless: bool = False) -> tuple[list[ReinventSession], list[ReinventSession]]:
    """Fetch the full re:Invent catalog and user wishlist.

    Strategy 1 (preferred, no browser): catalog.awsevents.com REST API +
      browser_cookie3 to read Chrome Profile 1 cookies.
    Strategy 2 (fallback): Playwright browser with Chrome profile / credential login.

    Returns (all_sessions, wishlisted_sessions).
    """
    # ── Strategy 1: direct REST API (fast, no browser window) ─────────────
    if _API_AVAILABLE:
        try:
            return await _fetch_catalog_via_api()
        except Exception as exc:
            print(f"[wishlist] API fetch failed ({exc}) — falling back to browser")

    if not _PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("playwright not installed — run: playwright install chromium")

    all_sessions: list[ReinventSession] = []
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        context: Optional[BrowserContext] = None
        page: Optional[Page] = None

        # ── Strategy 1: use existing Chrome profile (auto-login) ──────────
        chrome_profile = _CHROME_USER_DATA_DIR
        if chrome_profile.exists():
            try:
                print(f"[wishlist] Launching Chrome with your existing profile (auto-login)...")
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(chrome_profile),
                    channel="chrome",
                    headless=headless,
                    viewport={"width": 1440, "height": 900},
                    args=["--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble"],
                )
                page = context.pages[0] if context.pages else await context.new_page()
                print(f"[wishlist] Chrome launched — navigating to catalog...")
            except Exception as exc:
                print(f"[wishlist] Chrome profile launch failed ({exc}), falling back to Chromium + credentials...")
                context = None
                page = None

        # ── Strategy 2: fresh Chromium + credential login ─────────────────
        if context is None:
            browser = await pw.chromium.launch(headless=headless)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

        print(f"[wishlist] Opening catalog: {_CATALOG_URL}")
        await page.goto(_CATALOG_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_load_state("networkidle")

        # Detect "Event was not defined" — catalog not published yet
        body_text = (await page.inner_text("body")).strip().lower()
        if "event was not defined" in body_text or "not defined" in body_text:
            await context.close()
            raise RuntimeError(
                "CATALOG_NOT_OPEN: The re:Invent 2026 session catalog is not published yet.\n"
                "registration.awsevents.com returned: 'Event was not defined'\n"
                "AWS typically opens the catalog in mid-October — check back then."
            )

        # Handle login redirect (only needed if auto-login didn't work)
        current = page.url.lower()
        if any(kw in current for kw in ["sign-in", "login", "auth", "sso", "identity", "signin"]):
            print(f"[wishlist] Not logged in — filling credentials...")
            logged_in = await _wait_for_login(page, email, password)
            if not logged_in:
                print("[wishlist] WARNING: Could not confirm login. Continuing anyway...")
            # Navigate to catalog after login
            try:
                await page.goto(_CATALOG_URL, wait_until="networkidle", timeout=30000)
            except Exception:
                pass
            # Re-check for not-open
            body_text = (await page.inner_text("body")).strip().lower()
            if "event was not defined" in body_text:
                await context.close()
                raise RuntimeError("CATALOG_NOT_OPEN: re:Invent 2026 catalog not published yet.")
        else:
            print(f"[wishlist] Already logged in via Chrome profile ✓")

        # Try network intercept first (cleaner data)
        print("[wishlist] Attempting network intercept for session data...")
        intercepted = await _scrape_via_network_intercept(page)
        if intercepted:
            print(f"[wishlist] Intercepted {len(intercepted)} session records from API responses")
            for raw in intercepted:
                s = _normalise_raw(raw)
                if s and s.session_id not in seen_ids:
                    seen_ids.add(s.session_id)
                    all_sessions.append(s)

        # Always also do DOM scraping for wishlist flags (API may not include them)
        print("[wishlist] Scraping session cards (DOM)...")
        total_pages = await _get_total_pages(page)
        print(f"[wishlist] Detected {total_pages} catalog page(s)")

        for page_num in range(1, total_pages + 1):
            print(f"[wishlist]   Page {page_num}/{total_pages}...")
            dom_sessions = await _scrape_sessions_from_page(page)
            for raw in dom_sessions:
                s = _normalise_raw(raw)
                if s is None:
                    continue
                # Merge wishlist flag into already-captured session
                existing = next((x for x in all_sessions if x.title == s.title), None)
                if existing and s.is_wishlisted:
                    # Replace with wishlisted version
                    idx = all_sessions.index(existing)
                    all_sessions[idx] = ReinventSession(**{**asdict(existing), "is_wishlisted": True})
                elif s.session_id not in seen_ids:
                    seen_ids.add(s.session_id)
                    all_sessions.append(s)

            if page_num < total_pages:
                ok = await _click_next_page(page)
                if not ok:
                    print("[wishlist]   No next page button found — stopping pagination")
                    break

        # Also check "My Agenda" / "Wishlist" view if available
        print("[wishlist] Checking for dedicated wishlist/agenda view...")
        for wishlist_url_fragment in ["myagenda", "my-agenda", "wishlist", "saved", "bookmarks"]:
            wishlist_url = _CATALOG_URL.replace("event-catalog/page/eventCatalog", wishlist_url_fragment)
            try:
                await page.goto(wishlist_url, wait_until="networkidle", timeout=8000)
                wl_sessions = await _scrape_sessions_from_page(page)
                for raw in wl_sessions:
                    raw["is_wishlisted"] = True
                    s = _normalise_raw(raw)
                    if s is None:
                        continue
                    existing = next((x for x in all_sessions if x.title == s.title), None)
                    if existing:
                        idx = all_sessions.index(existing)
                        all_sessions[idx] = ReinventSession(**{**asdict(existing), "is_wishlisted": True})
                    elif s.session_id not in seen_ids:
                        seen_ids.add(s.session_id)
                        all_sessions.append(s)
                if wl_sessions:
                    print(f"[wishlist]   Found {len(wl_sessions)} sessions in {wishlist_url_fragment} view")
                    break
            except Exception:
                continue

        await context.close()

    wishlisted = [s for s in all_sessions if s.is_wishlisted]
    print(f"[wishlist] Total: {len(all_sessions)} sessions | {len(wishlisted)} wishlisted")
    return all_sessions, wishlisted


def save_results(all_sessions: list[ReinventSession], wishlisted: list[ReinventSession]) -> None:
    with open(_CATALOG_PATH, "w") as f:
        json.dump([asdict(s) for s in all_sessions], f, indent=2)
    with open(_WISHLIST_PATH, "w") as f:
        json.dump([asdict(s) for s in wishlisted], f, indent=2)
    print(f"[wishlist] Saved catalog → {_CATALOG_PATH}  |  wishlist → {_WISHLIST_PATH}")


def load_wishlist() -> list[ReinventSession]:
    if not _WISHLIST_PATH.exists():
        return []
    with open(_WISHLIST_PATH) as f:
        return [ReinventSession(**item) for item in json.load(f)]


def load_catalog() -> list[ReinventSession]:
    if not _CATALOG_PATH.exists():
        return []
    with open(_CATALOG_PATH) as f:
        return [ReinventSession(**item) for item in json.load(f)]
