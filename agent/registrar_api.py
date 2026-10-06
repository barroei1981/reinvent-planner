"""API-first registration with Playwright fallback.

Tries AWS Events registration API first (faster, more reliable).
Falls back to Playwright browser automation if API is blocked/unavailable.
"""

from __future__ import annotations

import asyncio
import httpx
from dataclasses import dataclass
from typing import Optional

from agent.catalog import Session


@dataclass
class RegistrationResult:
    session: Session
    status: str  # "registered" | "full" | "already_registered" | "not_open" | "error" | "api_blocked"
    message: str = ""
    via_api: bool = False  # True if registered via API, False if via Playwright


async def try_register_via_api(
    session: Session,
    email: str,
    password: str,
    cookies: Optional[dict] = None,
) -> RegistrationResult:
    """Attempt to register via AWS Events API.

    Returns:
        RegistrationResult with status:
        - "registered": successfully registered via API
        - "already_registered": already on schedule
        - "full": session at capacity
        - "not_open": registration not yet available
        - "api_blocked": API returned 401/403 (fall back to Playwright)
        - "error": other error (fall back to Playwright)
    """

    # Extract session ID from registration URL
    # Format: https://registration.awsevents.com/flow/.../session=<session_id>
    if not session.registration_url:
        return RegistrationResult(
            session=session,
            status="error",
            message="no registration URL",
            via_api=False,
        )

    # Parse session ID
    import re
    match = re.search(r'session[=:]([a-zA-Z0-9]+)', session.registration_url)
    if not match:
        return RegistrationResult(
            session=session,
            status="error",
            message="could not parse session ID from URL",
            via_api=False,
        )

    session_id = match.group(1)

    # Try registration API endpoints (common patterns for AWS Events / Rainfocus)
    api_endpoints = [
        f"https://registration.awsevents.com/api/v1/sessions/{session_id}/register",
        f"https://registration.awsevents.com/api/sessions/{session_id}/register",
        f"https://api.awsevents.com/reinvent2026/sessions/{session_id}/register",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Add cookies if provided (from previous Playwright session)
    if cookies:
        cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
        headers["Cookie"] = cookie_str

    timeout = httpx.Timeout(30.0)

    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for endpoint in api_endpoints:
            try:
                # Try POST to register
                response = await client.post(endpoint, json={"email": email})

                # Success cases
                if response.status_code == 200 or response.status_code == 201:
                    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}

                    # Check response for status
                    status_lower = str(data.get("status", "")).lower()
                    message_lower = str(data.get("message", "")).lower()

                    if "success" in status_lower or "registered" in message_lower:
                        return RegistrationResult(
                            session=session,
                            status="registered",
                            message="registered via API",
                            via_api=True,
                        )
                    elif "already" in message_lower or "duplicate" in message_lower:
                        return RegistrationResult(
                            session=session,
                            status="already_registered",
                            message="already registered (API)",
                            via_api=True,
                        )
                    elif "full" in message_lower or "capacity" in message_lower:
                        return RegistrationResult(
                            session=session,
                            status="full",
                            message="session full (API)",
                            via_api=True,
                        )
                    elif "not open" in message_lower or "not available" in message_lower:
                        return RegistrationResult(
                            session=session,
                            status="not_open",
                            message="registration not open (API)",
                            via_api=True,
                        )

                    # Assume success if 200/201 with no error indicators
                    return RegistrationResult(
                        session=session,
                        status="registered",
                        message=f"registered via API (status {response.status_code})",
                        via_api=True,
                    )

                # Auth errors - API is blocked or requires browser session
                elif response.status_code in (401, 403):
                    return RegistrationResult(
                        session=session,
                        status="api_blocked",
                        message=f"API auth required ({response.status_code}) - falling back to Playwright",
                        via_api=False,
                    )

                # 404 - try next endpoint
                elif response.status_code == 404:
                    continue

                # Other errors
                else:
                    continue

            except httpx.HTTPError:
                # Try next endpoint
                continue

    # All API endpoints failed - fall back to Playwright
    return RegistrationResult(
        session=session,
        status="api_blocked",
        message="all API endpoints failed - falling back to Playwright",
        via_api=False,
    )


async def extract_cookies_from_playwright(page) -> dict:
    """Extract cookies from Playwright page for API reuse."""
    cookies_list = await page.context.cookies()
    return {c["name"]: c["value"] for c in cookies_list}
