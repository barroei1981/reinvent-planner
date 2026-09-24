"""Scrape the re:Invent session catalog and user wishlist from registration.awsevents.com.

Flow:
  1. Open the registration site with Playwright.
  2. Log in via AWS Builder profile (email + password + possible MFA).
  3. Scrape ALL sessions from the session catalog (pagination).
  4. Identify sessions the user has bookmarked / added to wishlist.
  5. Save full catalog to reinvent_catalog.json and wishlist to wishlist.json.

The saved files are consumed by the scorer (wishlist items get a hard score of 1.0
and are locked into the schedule regardless of keyword score) and by the registrar
(wishlist items are registered first).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import async_playwright, Page, BrowserContext, Locator
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

# re:Invent 2026 catalog URL — update year suffix if needed
_CATALOG_URL = "https://registration.awsevents.com/flow/awsevents/reinvent26/sessioncatalog/page/page"
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
    speakers: list[str]
    is_wishlisted: bool
    capacity: Optional[str]
    registration_url: Optional[str]


async def _wait_for_login(page: Page, email: str, password: str) -> bool:
    """Handle the login flow. Returns True when logged in."""
    # Give the page time to settle
    await page.wait_for_load_state("networkidle")
    content = await page.content()

    # Check if already logged in
    if "session-catalog" in page.url.lower() or "sessioncatalog" in page.url.lower():
        if "sign-in" not in page.url.lower() and "login" not in page.url.lower():
            return True

    # Try filling email field
    for email_selector in ["input[type='email']", "input[name='email']", "#email", "input[placeholder*='email' i]", "input[placeholder*='Email' i]"]:
        try:
            field = page.locator(email_selector).first
            if await field.is_visible(timeout=2000):
                await field.fill(email)
                await field.press("Enter")
                await page.wait_for_load_state("networkidle")
                break
        except Exception:
            continue

    # Try password
    for pw_selector in ["input[type='password']", "input[name='password']", "#password"]:
        try:
            field = page.locator(pw_selector).first
            if await field.is_visible(timeout=3000):
                await field.fill(password)
                await field.press("Enter")
                await page.wait_for_load_state("networkidle")
                break
        except Exception:
            continue

    # Accept cookies / consent banners
    for btn_text in ["Accept", "Accept All", "I Agree", "Continue", "OK"]:
        try:
            btn = page.locator(f"button:has-text('{btn_text}')").first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await asyncio.sleep(0.5)
        except Exception:
            continue

    # Wait for catalog or dashboard to appear
    try:
        await page.wait_for_url(re.compile(r"(sessioncatalog|dashboard|myagenda|home)", re.I), timeout=15000)
        return True
    except Exception:
        pass

    # Check if we're past the login page
    current_url = page.url.lower()
    return "sign-in" not in current_url and "login" not in current_url and "auth" not in current_url


async def _scrape_sessions_from_page(page: Page) -> list[dict]:
    """Extract session cards from the current catalog page via JavaScript."""
    return await page.evaluate("""
        () => {
            const sessions = [];
            // Try multiple selector patterns Cvent uses
            const cards = document.querySelectorAll(
                '[data-testid="session-card"], .session-card, .evy-session-card, ' +
                '[class*="sessionCard"], [class*="session-item"], ' +
                '.fd-session, [data-component="session"]'
            );
            cards.forEach(card => {
                const getText = (sel) => {
                    const el = card.querySelector(sel);
                    return el ? el.textContent.trim() : null;
                };
                const getAttr = (sel, attr) => {
                    const el = card.querySelector(sel);
                    return el ? el.getAttribute(attr) : null;
                };

                // Wishlist / bookmark indicator
                const wishlisted = !!(
                    card.querySelector('[aria-label*="Remove from" i], [class*="bookmarked" i], [class*="wishlisted" i], [class*="saved" i], [data-saved="true"]') ||
                    card.classList.contains('saved') ||
                    card.classList.contains('bookmarked') ||
                    card.getAttribute('data-saved') === 'true' ||
                    card.getAttribute('data-bookmarked') === 'true'
                );

                // Extract data-* attributes Cvent often uses
                const sessionId = card.getAttribute('data-id') || card.getAttribute('data-session-id') || card.id || null;
                const title = getText('h2, h3, h4, [class*="title"], [class*="name"]');
                const desc = getText('[class*="description"], [class*="body"], p');
                const level = getText('[class*="level"], [class*="audience"]');
                const sessionType = getText('[class*="type"], [class*="format"], [class*="category"]');
                const venue = getText('[class*="venue"], [class*="building"], [class*="location"]');
                const room = getText('[class*="room"]');
                const date = getText('[class*="date"]');
                const time = getText('[class*="time"]');

                // Topics / tags
                const topicEls = card.querySelectorAll('[class*="topic"], [class*="tag"], [class*="interest"]');
                const topics = Array.from(topicEls).map(t => t.textContent.trim()).filter(Boolean);

                // Speakers
                const speakerEls = card.querySelectorAll('[class*="speaker"], [class*="presenter"]');
                const speakers = Array.from(speakerEls).map(s => s.textContent.trim()).filter(Boolean);

                // Registration / detail link
                const link = getAttr('a', 'href');

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
                        speakers,
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
        speakers=speakers,
        is_wishlisted=wishlisted,
        capacity=raw.get("capacity") or raw.get("seatsAvailable"),
        registration_url=reg_url if isinstance(reg_url, str) else None,
    )


async def sync_wishlist(email: str, password: str, *, headless: bool = False) -> tuple[list[ReinventSession], list[ReinventSession]]:
    """Log in, scrape the full re:Invent catalog and the user's wishlist.

    Returns (all_sessions, wishlisted_sessions).
    """
    if not _PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("playwright not installed — run: playwright install chromium")

    all_sessions: list[ReinventSession] = []
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context: BrowserContext = await browser.new_context(
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
            await browser.close()
            raise RuntimeError(
                "CATALOG_NOT_OPEN: The re:Invent 2026 session catalog is not published yet.\n"
                "registration.awsevents.com returned: 'Event was not defined'\n"
                "AWS typically opens the catalog in mid-October — check back then."
            )

        # Handle login redirect
        current = page.url.lower()
        if any(kw in current for kw in ["sign-in", "login", "auth", "sso", "identity"]):
            print(f"[wishlist] Login required — filling credentials...")
            logged_in = await _wait_for_login(page, email, password)
            if not logged_in:
                print("[wishlist] WARNING: Could not confirm login. Continuing anyway...")
            # Navigate to catalog after login
            await page.goto(_CATALOG_URL, wait_until="networkidle", timeout=30000)
            # Re-check for not-open
            body_text = (await page.inner_text("body")).strip().lower()
            if "event was not defined" in body_text:
                await browser.close()
                raise RuntimeError("CATALOG_NOT_OPEN: re:Invent 2026 catalog not published yet.")

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
            wishlist_url = _CATALOG_URL.replace("sessioncatalog/page/page", wishlist_url_fragment)
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

        await browser.close()

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
