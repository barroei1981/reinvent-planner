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
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from agent.catalog import Session
from agent.scheduler import ScheduledSession
from agent.registrar_api import try_register_via_api, extract_cookies_from_playwright

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

_ALREADY_REGISTERED_INDICATORS = [
    "you're registered",
    "you are registered",
    "you're on the waitlist",
    "you are on the waitlist",
    "already registered",
    "registration confirmed",
    "your seat is reserved",
    "you have reserved",
    "remove from schedule",  # If there's a "remove" button, you're already on it
    "cancel registration",
    "withdraw from",
]

_FULL_INDICATORS = [
    "session is full",
    "no seats available",
    "sold out",
    "at capacity",
]

_WAITLIST_AVAILABLE = [
    "join waitlist",
    "add to waitlist",
    "request waitlist",
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

    # CHECK IF ALREADY REGISTERED FIRST (before trying to click anything)
    for indicator in _ALREADY_REGISTERED_INDICATORS:
        if indicator in page_text:
            return RegistrationResult(session=session, status="already_registered", message=indicator)

    # TRY TO CLICK REGISTER BUTTON - if button exists, registration is open!
    for selector in _REGISTER_BUTTON_SELECTORS:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                print(f"  → Found button: {selector}")
                await btn.click()
                await page.wait_for_load_state("networkidle")
                confirm_text = (await page.content()).lower()

                # Check if we're now registered
                if "registered" in confirm_text or "confirmed" in confirm_text or "success" in confirm_text:
                    return RegistrationResult(session=session, status="registered")

                # Check if it's actually full after clicking
                for indicator in _FULL_INDICATORS:
                    if indicator in confirm_text:
                        return RegistrationResult(session=session, status="full", message=indicator)

                return RegistrationResult(session=session, status="registered", message="clicked (no explicit confirmation)")
        except Exception:
            continue

    # No button found - check why
    for indicator in _FULL_INDICATORS:
        if indicator in page_text:
            return RegistrationResult(session=session, status="full", message=indicator)

    # Check if waitlist is available
    for indicator in _WAITLIST_AVAILABLE:
        if indicator in page_text:
            # Try to find and click waitlist button
            try:
                waitlist_btn = page.locator("button:has-text('Waitlist')").first
                if await waitlist_btn.is_visible(timeout=2000):
                    await waitlist_btn.click()
                    await page.wait_for_load_state("networkidle")
                    return RegistrationResult(session=session, status="registered", message="joined waitlist")
            except Exception:
                pass
            return RegistrationResult(session=session, status="full", message="waitlist available but couldn't join")

    for indicator in _NOT_OPEN_INDICATORS:
        if indicator in page_text:
            return RegistrationResult(session=session, status="not_open", message=indicator)

    return RegistrationResult(session=session, status="error", message="register button not found")


def _cleanup_stale_chrome_profile():
    """Clean up stale Chrome processes and lock files (auto-healing)."""
    import subprocess

    # Check if lock file exists
    lock_file = _PLAYWRIGHT_CHROME_PROFILE / "SingletonLock"
    if lock_file.exists():
        print("[registrar] Detected stale Chrome profile lock - cleaning up...")

        # Kill any stuck Chrome processes using this profile
        try:
            subprocess.run(
                ["pkill", "-9", "-f", str(_PLAYWRIGHT_CHROME_PROFILE)],
                capture_output=True,
                timeout=5
            )
        except Exception:
            pass

        # Remove lock files
        try:
            lock_file.unlink(missing_ok=True)
            (lock_file.parent / "SingletonSocket").unlink(missing_ok=True)
            print("[registrar] Cleaned up stale locks")
        except Exception as e:
            print(f"[registrar] Warning: Could not remove locks: {e}")


async def _launch_context(pw, headless: bool, email: str, password: str):
    """Launch Chrome with dedicated Playwright profile (persists login across runs)."""

    # Use a dedicated profile for Playwright - keeps your main Chrome profile untouched
    playwright_profile = _PLAYWRIGHT_CHROME_PROFILE
    playwright_profile.mkdir(exist_ok=True)

    # Auto-heal: clean up any stale processes/locks before launching
    _cleanup_stale_chrome_profile()

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
# Note: "full", "not_open", and "error" are all retriable in watch mode
# Watch mode only stops when a session is successfully registered


def _write_status_file(results: dict[str, RegistrationResult], schedule: list[ScheduledSession]) -> None:
    """Write current registration status to /tmp/awsevents_status.json for HTML report."""
    status_data = {
        "last_check": datetime.now().isoformat(),
        "sessions": {},
        "summary": {
            "already_registered": 0,
            "full": 0,
            "not_open": 0,
            "error": 0,
            "backup": 0,
        }
    }

    for ss in schedule:
        event_id = ss.session.event_id
        result = results.get(event_id)

        if ss.backup:
            status = "backup"
            status_data["summary"]["backup"] += 1
        elif result:
            status = result.status
            if status in status_data["summary"]:
                status_data["summary"][status] += 1
        else:
            status = "unknown"

        status_data["sessions"][event_id] = {
            "title": ss.session.title,
            "status": status,
            "scheduled_start": ss.start.isoformat() if ss.start else "",
            "level": ss.session.learning_level or "Unknown",
            "backup": ss.backup or False,
        }

    Path("/tmp/awsevents_status.json").write_text(json.dumps(status_data, indent=2))


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
        self._api_working: Optional[bool] = None  # None = not tested, True = works, False = blocked
        self._cookies: Optional[dict] = None  # Cookies from Playwright for API reuse

    async def _try_register_hybrid(
        self,
        session: Session,
        page=None,
    ) -> RegistrationResult:
        """Try API first, fall back to Playwright if API is blocked.

        Returns RegistrationResult with via_api flag indicating method used.
        """

        # If API known to be blocked, skip straight to Playwright
        if self._api_working is False:
            if page is None:
                raise ValueError("Playwright page required when API is blocked")
            return await _try_register_session(page, session, self._email, self._password)

        # Try API first
        print(f"  → trying API registration...")
        api_result = await try_register_via_api(session, self._email, self._password, self._cookies)

        # API success - mark as working
        if api_result.status not in ("api_blocked", "error"):
            self._api_working = True
            return api_result

        # API blocked/failed - fall back to Playwright
        print(f"  → API blocked/failed, using Playwright...")
        self._api_working = False

        if page is None:
            raise ValueError("Playwright page required for fallback")

        # Extract cookies for future API attempts
        if self._cookies is None:
            self._cookies = await extract_cookies_from_playwright(page)

        return await _try_register_session(page, session, self._email, self._password)

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

            # Extract cookies once for API attempts
            if self._cookies is None:
                self._cookies = await extract_cookies_from_playwright(page)

            for ss in primaries:
                retries = 0
                while True:
                    # Try API first, fall back to Playwright
                    result = await self._try_register_hybrid(ss.session, page)
                    via_method = "API" if getattr(result, 'via_api', False) else "Playwright"
                    print(f"  [{result.status}] {ss.session.title[:60]} (via {via_method})")

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
                            backup_result = await self._try_register_hybrid(backup_ss.session, page)
                            via_method = "API" if getattr(backup_result, 'via_api', False) else "Playwright"
                            print(f"  [backup·{backup_result.status}] {backup_ss.session.title[:60]} (via {via_method})")
                            results.append(backup_result)
                    break

            await context.close()

        # Write status file for HTML report
        results_dict = {r.session.event_id: r for r in results}
        _write_status_file(results_dict, schedule)

        return results

    async def watch_and_register(self, schedule: list[ScheduledSession]) -> list[RegistrationResult]:
        """Poll indefinitely until every primary is registered.

        Watch mode keeps retrying until registration succeeds:
        - not_open → keep retrying (session not yet available)
        - full → keep retrying (seats may open up from cancellations)
        - error → keep retrying (transient errors)
        - registered/already_registered → done, stop retrying that session
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

            # Extract cookies once for API attempts
            if self._cookies is None:
                self._cookies = await extract_cookies_from_playwright(page)

            while pending:
                still_pending: list[ScheduledSession] = []

                for ss in pending:
                    # Try API first, fall back to Playwright
                    result = await self._try_register_hybrid(ss.session, page)
                    via_method = "API" if getattr(result, 'via_api', False) else "Playwright"
                    print(f"  [{result.status}] {ss.session.title[:60]} (via {via_method})")

                    # Success - stop retrying this session
                    if result.status in _SUCCESS:
                        results[ss.session.event_id] = result
                        print(f"    ✓ Successfully registered!")
                        continue

                    # Any non-success: keep retrying (full, not_open, error)
                    # Sessions can go from full → available when someone cancels
                    still_pending.append(ss)
                    results[ss.session.event_id] = result

                # Write status file for HTML report
                _write_status_file(results, schedule)

                if still_pending:
                    status_counts = {}
                    for ss in still_pending:
                        status = results.get(ss.session.event_id, RegistrationResult(ss.session, "unknown")).status
                        status_counts[status] = status_counts.get(status, 0) + 1

                    summary = ", ".join([f"{count} {status}" for status, count in status_counts.items()])
                    print(f"[registrar] {len(still_pending)} sessions waiting ({summary}) — sleeping {self._interval}s...")
                    await asyncio.sleep(self._interval)

                pending = still_pending

            await context.close()

        # Write final status
        _write_status_file(results, schedule)
        return list(results.values())
