"""Seat reservation for re:Invent 2026.

Truth comes from the catalog API (agent.seats): which sessions are reservable, which are walk-up
only or full, and what the account is actually enrolled in. The browser is used only to click
"Reserve a seat" on sessions the API says are reservable, and every booking is verified against
/api/myData afterwards. Schedule-conflict dialogs are always cancelled — an existing reservation
is never released automatically.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent.scheduler import ScheduledSession
from agent.seats import fetch_seat_status

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import Page, async_playwright
    _PLAYWRIGHT_AVAILABLE = True
    _UI_ERRORS: tuple[type[Exception], ...] = (PlaywrightError, asyncio.TimeoutError)
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    _UI_ERRORS = (asyncio.TimeoutError,)

_PLAYWRIGHT_CHROME_PROFILE = Path.home() / ".awsevents_chrome_profile"
_CATALOG_URL = "https://registration.awsevents.com/flow/awsevents/reinvent2026/event-catalog/page/eventCatalog"
_NAV_TIMEOUT_MS = 40_000
_ELEMENT_TIMEOUT_MS = 15_000
_LOGIN_TIMEOUT_MS = 300_000
_DEFAULT_WATCH_MAX_HOURS = 24

# Terminal states: nothing more a watch loop can do for these.
_FINAL = ("registered", "already_registered", "walk_up", "conflict")


@dataclass
class RegistrationResult:
    session: object
    status: str  # registered | already_registered | full | walk_up | conflict | unknown | error
    message: str = ""


def _api_cfg() -> dict:
    try:
        return yaml.safe_load(Path("config.yaml").read_text()).get("catalog_api", {})
    except (OSError, yaml.YAMLError):
        return {}


def _cleanup_stale_chrome_profile() -> None:
    """Kill leftover Chrome processes and remove lock files for our dedicated profile."""
    lock = _PLAYWRIGHT_CHROME_PROFILE / "SingletonLock"
    if not (lock.exists() or lock.is_symlink()):
        return
    print("[registrar] Stale Chrome profile lock found - cleaning up...")
    try:
        subprocess.run(["pkill", "-9", "-f", str(_PLAYWRIGHT_CHROME_PROFILE)], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[registrar] pkill failed: {exc}")
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (_PLAYWRIGHT_CHROME_PROFILE / name).unlink(missing_ok=True)


async def _ensure_logged_in(page: Page, email: str, password: str) -> None:
    """If we land on a sign-in page, fill credentials; fall back to manual completion."""
    if not any(x in page.url.lower() for x in ("signin.aws", "login", "sso", "auth")):
        return
    print("[registrar] Login page detected - filling credentials...")
    for sel, value, buttons in (
        ("input[type='email'], input[name='email'], #email", email, ["button:has-text('Continue')", "button[type='submit']"]),
        ("input[type='password'], input[name='password'], #password", password, ["button:has-text('Sign in')", "button:has-text('Continue')", "button[type='submit']"]),
    ):
        try:
            await page.wait_for_selector(sel, timeout=6000)
            await page.fill(sel, value)
            for b in buttons:
                try:
                    await page.click(b, timeout=2000)
                    break
                except _UI_ERRORS:
                    print(f"[registrar] login button not found: {b}")
            await page.wait_for_load_state("networkidle", timeout=15000)
        except _UI_ERRORS as exc:
            print(f"[registrar] login step skipped: {str(exc)[:80]}")
    if any(x in page.url.lower() for x in ("signin.aws", "login", "sso", "auth")):
        print("[registrar] Automated login could not complete - finish it in the browser (up to 5 min).")
        try:
            await page.wait_for_url(re.compile(r"(reinvent2026|eventCatalog|registration\.awsevents)", re.IGNORECASE), timeout=_LOGIN_TIMEOUT_MS)
        except _UI_ERRORS:
            print("[registrar] manual login not completed in time")


async def _reserve_one(page: Page, code: str) -> tuple[str, str]:
    """Click 'Reserve a seat' on the card for `code`. Returns (outcome, message); outcome in ok|conflict|error."""
    box = page.get_by_placeholder("Search catalog")
    await box.fill(code)
    await box.press("Enter")
    card = page.locator("li.catalog-result", has=page.locator(f".catalog-result-title-text:has-text('({code})')"))
    try:
        await card.first.wait_for(timeout=_ELEMENT_TIMEOUT_MS)
    except _UI_ERRORS:
        return "error", "session card not found"
    await page.wait_for_timeout(500)
    btn = card.first.locator("button.session-main-action", has_text="Reserve a seat")
    if not await btn.first.is_visible():
        actions = (await card.first.locator(".session-actions").first.inner_text()).replace("\n", " | ")
        return "error", f"no Reserve button ({actions[:80]})"
    await btn.first.click(timeout=_ELEMENT_TIMEOUT_MS)
    await page.wait_for_timeout(3000)

    if await page.get_by_text("Schedule Conflict").first.is_visible():
        try:
            await page.get_by_role("button", name="Cancel").last.click(timeout=5000)
        except _UI_ERRORS:
            await page.keyboard.press("Escape")
        return "conflict", "conflicts with an existing reservation (left untouched)"
    actions = await card.first.locator(".session-actions").first.inner_text()
    if "unreserve" in actions.lower():
        return "ok", "reserved"
    return "error", f"unexpected state after click ({actions.replace(chr(10), ' | ')[:80]})"


class Registrar:
    def __init__(self, reg_cfg: dict, email: str, password: str, api_cfg: dict | None = None) -> None:
        self._cfg = reg_cfg
        self._email = email
        self._password = password
        self._api = api_cfg or _api_cfg()
        self._headless = bool(reg_cfg.get("headless", True))
        self._interval = int(reg_cfg.get("watch_interval_seconds", 600))
        self._max_hours = float(reg_cfg.get("watch_max_hours", _DEFAULT_WATCH_MAX_HOURS))

    async def _status(self, schedule: list[ScheduledSession]) -> dict:
        return await fetch_seat_status(schedule, self._api.get("rf_profile_id", ""), self._api.get("rf_widget_id", ""))

    async def _book(self, wanted: list[tuple[str, str]]) -> dict[str, tuple[str, str]]:
        """Reserve [(event_id, code)] in one browser session. Returns {event_id: (outcome, message)}."""
        if not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("playwright not installed - run: uv add playwright && playwright install chromium")
        _PLAYWRIGHT_CHROME_PROFILE.mkdir(exist_ok=True)
        _cleanup_stale_chrome_profile()
        out: dict[str, tuple[str, str]] = {}
        async with async_playwright() as pw:
            ctx = await asyncio.wait_for(
                pw.chromium.launch_persistent_context(
                    user_data_dir=str(_PLAYWRIGHT_CHROME_PROFILE), channel="chrome", headless=self._headless,
                    viewport={"width": 1440, "height": 1200}, args=["--no-first-run", "--no-default-browser-check"],
                ),
                timeout=60,
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(_CATALOG_URL, wait_until="networkidle", timeout=_NAV_TIMEOUT_MS)
                await _ensure_logged_in(page, self._email, self._password)
                for event_id, code in wanted:
                    try:
                        out[event_id] = await asyncio.wait_for(_reserve_one(page, code), timeout=90)
                    except _UI_ERRORS as exc:
                        out[event_id] = ("error", str(exc)[:120])
            finally:
                await ctx.close()
        return out

    async def register_schedule(
        self, schedule: list[ScheduledSession], only: set[str] | None = None
    ) -> list[RegistrationResult]:
        """Book reservable sessions; if `only` is given, restrict booking to those event ids."""
        data = await self._status(schedule)
        primaries = [ss for ss in schedule if not ss.backup]
        backups = {ss.start.isoformat()[:16]: ss for ss in schedule if ss.backup}
        info = data["sessions"]

        wanted: list[tuple[str, str]] = []
        for ss in sorted(primaries, key=lambda x: -x.session.score):
            eid = ss.session.event_id
            slot_backup = backups.get(ss.start.isoformat()[:16])
            pick = ss
            if info[eid]["status"] in ("full", "walk_up", "unknown") and slot_backup and info[slot_backup.session.event_id]["status"] == "reservable":
                pick = slot_backup
            peid = pick.session.event_id
            if info[peid]["status"] == "reservable" and info[peid].get("code") and (only is None or peid in only):
                wanted.append((peid, info[peid]["code"]))

        booked = await self._book(wanted) if wanted else {}
        if booked:
            data = await self._status(schedule)  # authoritative: did the account really get enrolled?
            info = data["sessions"]

        results: list[RegistrationResult] = []
        for ss in schedule:
            eid = ss.session.event_id
            status, msg = info[eid]["status"], ""
            if eid in booked:
                outcome, msg = booked[eid]
                if status == "enrolled":
                    status = "registered"
                elif outcome == "conflict":
                    status = "conflict"
                else:
                    status = "error"
                    msg = msg or "reserve click not confirmed by /api/myData"
            elif status == "enrolled":
                status = "already_registered"
            results.append(RegistrationResult(session=ss.session, status=status, message=msg))
        return results

    async def watch_and_register(self, schedule: list[ScheduledSession]) -> list[RegistrationResult]:
        deadline = time.monotonic() + self._max_hours * 3600
        results: list[RegistrationResult] = []
        while True:
            results = await self.register_schedule(schedule)
            pending = [r for r in results if r.status not in _FINAL and not _is_backup(schedule, r)]
            print(f"[watch] {len(results) - len(pending)} settled, {len(pending)} still waiting")
            if not pending or time.monotonic() >= deadline:
                return results
            await asyncio.sleep(self._interval)


def _is_backup(schedule: list[ScheduledSession], r: RegistrationResult) -> bool:
    return any(ss.backup and ss.session.event_id == r.session.event_id for ss in schedule)
