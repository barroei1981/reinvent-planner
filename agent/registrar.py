"""Playwright-based automatic seat registration for re:Invent 2026.

Flow:
  1. Log in to builder.aws.com (AWS Builder profile / SSO).
  2. For each session in priority order:
     a. Navigate to the session's registration_url.
     b. Find and click the "Reserve Seat" / "Register" button.
     c. Handle: already registered, session full, not yet open.
  3. In watch mode, poll every N seconds until seats open, then register.

Environment:
  AWSEVENTS_PASSWORD — your AWS Builder account password (set in .env)
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

from agent.catalog import Session
from agent.scheduler import ScheduledSession

try:
    from playwright.async_api import async_playwright, Page, BrowserContext
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


_LOGIN_URL = "https://builder.aws.com"
_REINVENT_CATALOG_URL = "https://registration.awsevents.com/flow/awsevents/reinvent26/sessioncatalog/page/page"

_REGISTER_BUTTON_SELECTORS = [
    "button:has-text('Reserve Seat')",
    "button:has-text('Register')",
    "button:has-text('Add to Schedule')",
    "a:has-text('Reserve Seat')",
    "a:has-text('Register Now')",
]

_FULL_INDICATORS = [
    "session is full",
    "no seats available",
    "waitlist",
    "sold out",
]

_NOT_OPEN_INDICATORS = [
    "registration not yet open",
    "coming soon",
    "not available yet",
    "opens on",
]


@dataclass
class RegistrationResult:
    session: Session
    status: str       # "registered" | "full" | "already_registered" | "not_open" | "error"
    message: str = ""


async def _login(page: Page, email: str, password: str) -> bool:
    """Log in to AWS Builder profile. Returns True on success."""
    await page.goto(_LOGIN_URL, wait_until="networkidle")

    # Accept cookies if present
    try:
        await page.click("button:has-text('Accept')", timeout=3000)
    except Exception:
        pass

    # Fill email
    try:
        await page.fill("input[type='email'], input[name='email'], #email", email, timeout=5000)
        await page.press("input[type='email'], input[name='email'], #email", "Enter")
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass

    # Fill password
    try:
        await page.fill("input[type='password'], input[name='password'], #password", password, timeout=5000)
        await page.press("input[type='password'], input[name='password'], #password", "Enter")
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass

    # Confirm we're logged in by checking for user-specific element
    try:
        await page.wait_for_selector("[data-testid='user-menu'], .user-profile, .logged-in", timeout=8000)
        return True
    except Exception:
        return False


async def _try_register_session(
    page: Page, session: Session
) -> RegistrationResult:
    """Navigate to a session and attempt to reserve a seat."""
    url = session.registration_url
    if not url:
        return RegistrationResult(session=session, status="error", message="no registration URL")

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception as exc:
        return RegistrationResult(session=session, status="error", message=str(exc))

    page_text = (await page.content()).lower()

    for indicator in _NOT_OPEN_INDICATORS:
        if indicator in page_text:
            return RegistrationResult(session=session, status="not_open", message=indicator)

    for indicator in _FULL_INDICATORS:
        if indicator in page_text:
            return RegistrationResult(session=session, status="full", message=indicator)

    if "already registered" in page_text or "you are registered" in page_text:
        return RegistrationResult(session=session, status="already_registered")

    # Find and click register button
    for selector in _REGISTER_BUTTON_SELECTORS:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_load_state("networkidle")
                confirm_text = (await page.content()).lower()
                if "registered" in confirm_text or "confirmed" in confirm_text or "success" in confirm_text:
                    return RegistrationResult(session=session, status="registered")
                # If no clear confirmation, treat as registered
                return RegistrationResult(session=session, status="registered", message="clicked (no explicit confirmation)")
        except Exception:
            continue

    return RegistrationResult(session=session, status="error", message="register button not found")


class Registrar:
    def __init__(self, reg_config: dict, email: str, password: str) -> None:
        if not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "playwright is not installed. Run: uv sync && playwright install chromium"
            )
        self._email = email
        self._password = password
        self._headless: bool = reg_config.get("headless", False)
        self._interval: int = int(reg_config.get("watch_interval_seconds", 30))
        self._max_retries: int = int(reg_config.get("max_retries", 3))
        self._max_sessions: int = int(reg_config.get("max_sessions_to_register", 25))

    async def register_schedule(
        self,
        schedule: list[ScheduledSession],
        *,
        watch: bool = False,
    ) -> list[RegistrationResult]:
        """Register for sessions in the given schedule.

        If watch=True, poll until sessions open and then register.
        Returns list of RegistrationResult for each attempt.
        """
        # Build priority-ordered session list (score desc)
        sessions = sorted(
            [ss.session for ss in schedule if ss.session.registration_url],
            key=lambda s: s.score,
            reverse=True,
        )[: self._max_sessions]

        results: list[RegistrationResult] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self._headless)
            context: BrowserContext = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            )
            page = await context.new_page()

            print(f"[registrar] Logging in as {self._email}...")
            logged_in = await _login(page, self._email, self._password)
            if not logged_in:
                print("[registrar] WARNING: Login may not have succeeded — proceeding anyway")

            for session in sessions:
                retries = 0
                while retries <= self._max_retries:
                    result = await _try_register_session(page, session)
                    print(f"  [{result.status}] {session.title[:60]}")
                    if result.status == "not_open" and watch:
                        print(f"    → not open yet, will retry in {self._interval}s")
                        await asyncio.sleep(self._interval)
                        retries += 1
                        continue
                    results.append(result)
                    break

            await browser.close()

        return results

    async def watch_and_register(self, schedule: list[ScheduledSession]) -> list[RegistrationResult]:
        """Continuously poll until sessions open, then register. Blocks until done."""
        sessions_todo = [
            ss.session for ss in schedule if ss.session.registration_url
        ][: self._max_sessions]
        sessions_todo.sort(key=lambda s: s.score, reverse=True)

        results: dict[str, RegistrationResult] = {}
        pending = list(sessions_todo)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self._headless)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            )
            page = await context.new_page()

            print(f"[registrar] Logging in as {self._email}...")
            await _login(page, self._email, self._password)

            while pending:
                still_pending = []
                for session in pending:
                    result = await _try_register_session(page, session)
                    print(f"  [{result.status}] {session.title[:60]}")
                    if result.status == "not_open":
                        still_pending.append(session)
                    else:
                        results[session.event_id] = result

                if still_pending:
                    print(f"[registrar] {len(still_pending)} sessions not yet open. Sleeping {self._interval}s...")
                    await asyncio.sleep(self._interval)

                pending = still_pending

            await browser.close()

        return list(results.values())
