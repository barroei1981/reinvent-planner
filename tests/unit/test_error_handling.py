"""Narrowed exception handling: expected failures are tolerated, unexpected ones propagate."""
import sqlite3

import browser_cookie3
import pytest

from agent import wishlist
from agent.serve import _State
from agent.wizard import _dicts_to_scheduled


class _Unexpected(Exception):
    pass


def _patch_cookie_reader(monkeypatch, tmp_path, error):
    db = tmp_path / "Cookies"
    db.write_text("")
    monkeypatch.setattr(wishlist, "_CHROME_PROFILE_1_COOKIES", db)
    monkeypatch.setattr(wishlist, "_load_cookie_file", lambda: ({"from": "file"}, "jwt"))

    def boom(**_kwargs):
        raise error

    monkeypatch.setattr(wishlist._bc3, "chrome", boom)


@pytest.mark.parametrize(
    "error",
    [sqlite3.OperationalError("locked"), OSError("denied"), browser_cookie3.BrowserCookieError("v20")],
)
def test_unreadable_chrome_cookies_fall_back_to_cookie_file(monkeypatch, tmp_path, error):
    _patch_cookie_reader(monkeypatch, tmp_path, error)
    assert wishlist._load_chrome_profile1_cookies() == ({"from": "file"}, "jwt")


def test_unexpected_cookie_error_is_not_swallowed(monkeypatch, tmp_path):
    _patch_cookie_reader(monkeypatch, tmp_path, _Unexpected("bug"))
    with pytest.raises(_Unexpected):
        wishlist._load_chrome_profile1_cookies()


def _item(**over):
    base = {
        "event_id": "e1", "title": "T", "start_date": "2026-12-01",
        "scheduled_start": "2026-12-01T09:00:00", "scheduled_end": "2026-12-01T10:00:00",
    }
    return {**base, **over}


def test_schedule_item_with_bad_times_is_skipped():
    good, bad = _item(), _item(event_id="e2", scheduled_start="not-a-time")
    assert [s.session.event_id for s in _dicts_to_scheduled([good, bad])] == ["e1"]


def test_state_is_per_instance():
    a, b = _State(), _State()
    a.config["k"] = 1
    a.scored.append("x")
    assert b.config == {} and b.scored == []
