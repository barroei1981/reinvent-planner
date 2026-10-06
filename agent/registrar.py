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
_PLAYWRIGHT_CHROME_PROFILE = Path.home() / ".awsevents_chrome_profile"
_REGISTRATION_BASE = "https://registration.awsevents.com"

_REGISTER_BUTTON_SELECTORS = [
    "button:has-text('Reserve Seat')",
    "button:has-text('Register')",
    "button:has-text('Add to Schedule')",
    "button:has-text('Reserve')",
    "button:has-text('Sign Up')",
    "a:has-text('Reserve Seat')",
    "a:has-text('Register Now')",
    "a:has-text('Register')",
    "[data-testid*='register']",
    "[data-testid*='reserve']",
    ".register-button",
    ".reserve-button",
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

    # Debug: check what status indicators are present
    has_reserved = "reserved" in page_text or "reserve seat" in page_text
    has_walk_in = "walk-in" in page_text or "walk in" in page_text or "waitlist" in page_text
    has_registered = "you are registered" in page_text or "already registered" in page_text or "you're registered" in page_text

    # If walk-in/waitlist, treat as NOT registered - we want to upgrade to reserved!
    if has_walk_in:
        print(f"    → walk-in/waitlist detected - will try to reserve seat")
        # Continue to button clicking to upgrade to reserved
    elif has_registered and not has_walk_in:
        # Only skip if truly registered (not walk-in)
        return RegistrationResult(session=session, status="already_registered", message="confirmed registration")

    # TRY TO CLICK REGISTER BUTTON FIRST - if button exists, registration is open!
    for selector in _REGISTER_BUTTON_SELECTORS:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                print(f"  → Found button: {selector}")
                await btn.click()
                await page.wait_for_load_state("networkidle")
                confirm_text = (await page.content()).lower()
                if "registered" in confirm_text or "confirmed" in confirm_text or "success" in confirm_text:
                    return RegistrationResult(session=session, status="registered")
                # Check if it's actually full after clicking
                for indicator in _FULL_INDICATORS:
                    if indicator in confirm_text:
                        return RegistrationResult(session=session, status="full", message=indicator)
                return RegistrationResult(session=session, status="registered", message="clicked (no explicit confirmation)")
        except Exception:
            continue

    # No button found - NOW check why
    for indicator in _FULL_INDICATORS:
        if indicator in page_text:
            return RegistrationResult(session=session, status="full", message=indicator)

    for indicator in _NOT_OPEN_INDICATORS:
        if indicator in page_text:
            return RegistrationResult(session=session, status="not_open", message=indicator)

    return RegistrationResult(session=session, status="error", message="register button not found")


async def _launch_context(pw, headless: bool, email: str, password: str):
    """Launch Chrome with dedicated Playwright profile (persists login across runs)."""

    # Use a dedicated profile for Playwright - keeps your main Chrome profile untouched
    playwright_profile = _PLAYWRIGHT_CHROME_PROFILE
    playwright_profile.mkdir(exist_ok=True)

    first_run = not (playwright_profile / "Default").exists()
    if first_run:
        print("[registrar] First run - automatic login will happen on first session")
    else:
        print("[registrar] Using saved login from previous run")

    print(f"[registrar] Launching Chrome...")
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(playwright_profile),
        channel="chrome",
        headless=headless,
        viewport={"width": 1440, "height": 900},
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
        ],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    print("[registrar] ✓ Chrome ready")

    # Pre-warm with login on first run
    if first_run:
        try:
            print("[registrar] Setting up login...")
            await page.goto(_REGISTRATION_BASE, wait_until="domcontentloaded", timeout=20000)
            await _ensure_logged_in(page, email, password)
            print("[registrar] ✓ Login complete")
        except Exception as e:
            print(f"[registrar] Pre-login warning: {e} - will retry on first session")

    return context, page


_SUCCESS = ("registered", "already_registered")
_PERMANENT_FAIL = ("full", "error")   # not_open is retriable; these are not


def _backup_map(schedule: list[ScheduledSession]) -> dict[str, ScheduledSession]:
    """Build {slot_key → backup ScheduledSession} from a schedule list."""
    return {
        ss.start.isoformat()[:16]: ss
        for ss in schedule
        if ss.backup and ss.session.registration_url
    }


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

    async def _attempt_with_backup(
        self,
        page,
        ss: ScheduledSession,
        backups: dict[str, ScheduledSession],
        results: list[RegistrationResult],
        *,
        label: str = "",
    ) -> bool:
        """Try to register one session; on permanent failure try its backup.

        Returns True if the slot ended up with a successful registration.
        """
        session = ss.session
        result = await _try_register_session(page, session, self._email, self._password)
        tag = f"[{label}] " if label else ""
        print(f"  {tag}[{result.status}] {session.title[:60]}")
        results.append(result)

        if result.status in _SUCCESS:
            return True

        if result.status in _PERMANENT_FAIL:
            slot = ss.start.isoformat()[:16]
            backup_ss = backups.get(slot)
            if backup_ss:
                print(f"  → primary {result.status} — trying backup: {backup_ss.session.title[:55]}")
                backup_result = await _try_register_session(
                    page, backup_ss.session, self._email, self._password
                )
                print(f"  [backup·{backup_result.status}] {backup_ss.session.title[:60]}")
                results.append(backup_result)
                return backup_result.status in _SUCCESS

        return False

    async def register_schedule(
        self,
        schedule: list[ScheduledSession],
        *,
        watch: bool = False,
    ) -> list[RegistrationResult]:
        """Register all primary sessions; fall back to backups on permanent failure.

        With watch=True retries not-yet-open sessions up to max_retries times
        before attempting the backup.
        """
        primaries = sorted(
            [ss for ss in schedule if ss.session.registration_url and not ss.backup],
            key=lambda ss: ss.session.score,
            reverse=True,
        )[: self._max_sessions]
        backups = _backup_map(schedule)
        results: list[RegistrationResult] = []

        async with async_playwright() as pw:
            context, page = await _launch_context(pw, self._headless, self._email, self._password)

            for ss in primaries:
                retries = 0
                while True:
                    result = await _try_register_session(page, ss.session, self._email, self._password)
                    print(f"  [{result.status}] {ss.session.title[:60]}")

                    if result.status == "not_open" and watch and retries < self._max_retries:
                        print(f"    → not open yet, retry {retries + 1}/{self._max_retries} in {self._interval}s")
                        await asyncio.sleep(self._interval)
                        retries += 1
                        continue

                    results.append(result)
                    # On permanent failure (including not_open after max retries), try backup
                    if result.status not in _SUCCESS:
                        slot = ss.start.isoformat()[:16]
                        backup_ss = backups.get(slot)
                        if backup_ss:
                            print(f"  → primary {result.status} — trying backup: {backup_ss.session.title[:55]}")
                            backup_result = await _try_register_session(
                                page, backup_ss.session, self._email, self._password
                            )
                            print(f"  [backup·{backup_result.status}] {backup_ss.session.title[:60]}")
                            results.append(backup_result)
                    break

            await context.close()

        return results

    async def watch_and_register(self, schedule: list[ScheduledSession]) -> list[RegistrationResult]:
        """Poll indefinitely until every primary is registered (or permanently fails).

        - not_open  → keep retrying on next poll cycle
        - full/error → try backup immediately; stop retrying primary
        - registered/already_registered → slot done, skip backup
        """
        primaries = sorted(
            [ss for ss in schedule if ss.session.registration_url and not ss.backup],
            key=lambda ss: ss.session.score,
            reverse=True,
        )[: self._max_sessions]
        backups = _backup_map(schedule)

        results: dict[str, RegistrationResult] = {}
        # pending holds ScheduledSessions still waiting to open
        pending: list[ScheduledSession] = list(primaries)

        async with async_playwright() as pw:
            context, page = await _launch_context(pw, self._headless, self._email, self._password)

            while pending:
                still_pending: list[ScheduledSession] = []

                for ss in pending:
                    result = await _try_register_session(page, ss.session, self._email, self._password)
                    print(f"  [{result.status}] {ss.session.title[:60]}")

                    if result.status == "not_open":
                        still_pending.append(ss)   # retry next cycle
                        continue

                    results[ss.session.event_id] = result

                    if result.status in _PERMANENT_FAIL:
                        slot = ss.start.isoformat()[:16]
                        backup_ss = backups.get(slot)
                        if backup_ss:
                            print(f"  → primary {result.status} — trying backup: {backup_ss.session.title[:55]}")
                            backup_result = await _try_register_session(
                                page, backup_ss.session, self._email, self._password
                            )
                            print(f"  [backup·{backup_result.status}] {backup_ss.session.title[:60]}")
                            results[backup_ss.session.event_id] = backup_result
                            if backup_result.status == "not_open":
                                still_pending.append(backup_ss)  # backup also not open yet

                if still_pending:
                    print(f"[registrar] {len(still_pending)} session(s) not yet open — sleeping {self._interval}s...")
                    await asyncio.sleep(self._interval)

                pending = still_pending

            await context.close()

        return list(results.values())
