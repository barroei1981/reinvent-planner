"""Playwright-based automatic seat registration for re:Invent 2026.

Flow:
  1. Launch Chrome with your existing Profile 1 (already authenticated from sync-wishlist).
     Falls back to fresh Chromium + credential login if the profile is unavailable.
  2. For each session in priority order:
     a. Navigate to the session's registration_url.
     b. Find and click the "Reserve Seat" / "Register" button.
     c. Handle: already registered, session full, not yet open.
  3. In watch mode, poll every N seconds until seats open, then register.

Environment:
  AWSEVENTS_PASSWORD — your AWS Builder account password (set in .env, used as fallback only)
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent.catalog import Session
from agent.scheduler import ScheduledSession

try:
    from playwright.async_api import async_playwright, Page, BrowserContext
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_CHROME_USER_DATA_DIR = Path.home() / "Library/Application Support/Google/Chrome"
_REGISTRATION_BASE = "https://registration.awsevents.com"

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
    "at capacity",
]

_NOT_OPEN_INDICATORS = [
    "registration not yet open",
    "coming soon",
    "not available yet",
    "opens on",
    "check back",
]


@dataclass
class RegistrationResult:
    session: Session
    status: str       # "registered" | "full" | "already_registered" | "not_open" | "error"
    message: str = ""


async def _ensure_logged_in(page: Page, email: str, password: str) -> None:
    """If we land on a login page, complete the AWS Builder ID login flow.

    Called after navigating to each session URL so re-auth is handled automatically
    if the session cookie expires mid-run.
    """
    url = page.url.lower()
    if not ("signin.aws" in url or "login" in url or "sso" in url or "auth" in url):
        return  # already on the target page

    print("[registrar] Login page detected — filling credentials...")

    # Email step
    try:
        email_sel = "input[type='email'], input[name='email'], #email, input[placeholder*='email' i]"
        await page.wait_for_selector(email_sel, timeout=6000)
        await page.fill(email_sel, email)
        for cont in ["button:has-text('Continue')", "input[type='submit']", "button[type='submit']"]:
            try:
                await page.click(cont, timeout=2000)
                break
            except Exception:
                pass
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass

    # Password step
    try:
        pw_sel = "input[type='password'], input[name='password'], #password"
        await page.wait_for_selector(pw_sel, timeout=6000)
        await page.fill(pw_sel, password)
        for cont in ["button:has-text('Sign in')", "button:has-text('Continue')", "input[type='submit']"]:
            try:
                await page.click(cont, timeout=2000)
                break
            except Exception:
                pass
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass

    # If still on login, prompt for manual completion
    if any(x in page.url.lower() for x in ("signin.aws", "login", "sso", "auth")):
        print(
            "\n[registrar] ──────────────────────────────────────────────────\n"
            "[registrar] Automated login could not complete.\n"
            "[registrar] Please finish logging in manually in the browser window.\n"
            "[registrar] Waiting up to 3 minutes...\n"
            "[registrar] ──────────────────────────────────────────────────"
        )
        try:
            await page.wait_for_url(
                re.compile(r"(reinvent2026|eventCatalog|event-catalog|registration\.awsevents)", re.I),
                timeout=300_000,  # 5 minutes — matches wishlist.py
            )
        except Exception:
            pass


async def _try_register_session(page: Page, session: Session, email: str, password: str) -> RegistrationResult:
    """Navigate to a session and attempt to reserve a seat."""
    url = session.registration_url
    if not url:
        return RegistrationResult(session=session, status="error", message="no registration URL")

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception as exc:
        return RegistrationResult(session=session, status="error", message=str(exc))

    # Handle any login redirect
    await _ensure_logged_in(page, email, password)

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
                return RegistrationResult(session=session, status="registered", message="clicked (no explicit confirmation)")
        except Exception:
            continue

    return RegistrationResult(session=session, status="error", message="register button not found")


async def _launch_context(pw, headless: bool, email: str, password: str):
    """Launch Chrome Profile 1 (preferred) or fall back to fresh Chromium."""
    chrome_profile = _CHROME_USER_DATA_DIR
    if chrome_profile.exists():
        try:
            print("[registrar] Launching Chrome with your existing profile (auto-login)...")
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(chrome_profile),
                channel="chrome",
                headless=headless,
                viewport={"width": 1440, "height": 900},
                args=["--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            return context, page
        except Exception as exc:
            print(f"[registrar] Chrome profile launch failed ({exc}) — falling back to Chromium + credentials...")

    # Fallback: fresh Chromium, login will happen on first redirect
    browser = await pw.chromium.launch(headless=headless)
    context = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    # Pre-warm: navigate to registration so auth cookie is established before session pages
    try:
        print(f"[registrar] Pre-warming session on {_REGISTRATION_BASE}...")
        await page.goto(_REGISTRATION_BASE, wait_until="networkidle", timeout=20000)
        await _ensure_logged_in(page, email, password)
    except Exception:
        pass
    return context, page


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

        Primary sessions are registered first. If a primary fails (full/error),
        the backup session for that time slot is tried automatically.
        If watch=True, retries not-yet-open sessions until they open.
        """
        primaries = sorted(
            [ss for ss in schedule if ss.session.registration_url and not ss.backup],
            key=lambda ss: ss.session.score,
            reverse=True,
        )[: self._max_sessions]

        # Map slot → backup session for fallback
        backups: dict[str, ScheduledSession] = {}
        for ss in schedule:
            if ss.backup and ss.session.registration_url:
                backups[ss.start.isoformat()[:16]] = ss

        results: list[RegistrationResult] = []

        async with async_playwright() as pw:
            context, page = await _launch_context(pw, self._headless, self._email, self._password)

            for ss in primaries:
                session = ss.session
                retries = 0
                registered = False
                while retries <= self._max_retries:
                    result = await _try_register_session(page, session, self._email, self._password)
                    print(f"  [{result.status}] {session.title[:60]}")
                    if result.status == "not_open" and watch:
                        print(f"    → not open yet, will retry in {self._interval}s")
                        await asyncio.sleep(self._interval)
                        retries += 1
                        continue
                    results.append(result)
                    registered = result.status in ("registered", "already_registered")
                    break

                # Try backup if primary failed
                if not registered:
                    slot = ss.start.isoformat()[:16]
                    backup_ss = backups.get(slot)
                    if backup_ss:
                        print(f"  [primary failed] Trying backup: {backup_ss.session.title[:55]}")
                        backup_result = await _try_register_session(
                            page, backup_ss.session, self._email, self._password
                        )
                        print(f"  [{backup_result.status}] {backup_ss.session.title[:60]}")
                        results.append(backup_result)

            await context.close()

        return results

    async def watch_and_register(self, schedule: list[ScheduledSession]) -> list[RegistrationResult]:
        """Continuously poll until all sessions open, then register. Blocks until done."""
        sessions_todo = sorted(
            [ss.session for ss in schedule if ss.session.registration_url],
            key=lambda s: s.score,
            reverse=True,
        )[: self._max_sessions]

        results: dict[str, RegistrationResult] = {}
        pending = list(sessions_todo)

        async with async_playwright() as pw:
            context, page = await _launch_context(pw, self._headless, self._email, self._password)

            while pending:
                still_pending = []
                for session in pending:
                    result = await _try_register_session(page, session, self._email, self._password)
                    print(f"  [{result.status}] {session.title[:60]}")
                    if result.status == "not_open":
                        still_pending.append(session)
                    else:
                        results[session.event_id] = result

                if still_pending:
                    print(f"[registrar] {len(still_pending)} sessions not yet open. Sleeping {self._interval}s...")
                    await asyncio.sleep(self._interval)

                pending = still_pending

            await context.close()

        return list(results.values())
