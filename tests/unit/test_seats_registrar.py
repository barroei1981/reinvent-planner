import asyncio
from datetime import datetime, timedelta, timezone

from agent.catalog import Session
from agent.registrar import Registrar
from agent.scheduler import ScheduledSession
from agent.seats import _is_walk_up, classify, find_openings


def _item(walk_up=False):
    return {"attributevalues": [{"attribute": "Walk up only session"}] if walk_up else []}


def _time(seats=5, waitlist=0, access=("a",)):
    return {"seatsRemaining": seats, "waitlistRemaining": waitlist, "scheduleAccess": list(access)}


def test_classify_each_status():
    assert classify(_item(), _time(), enrolled=True) == "enrolled"
    assert classify(None, None, enrolled=False) == "unknown"
    assert classify(_item(walk_up=True), _time(), False) == "walk_up"
    assert classify(_item(), _time(access=()), False) == "walk_up"  # empty scheduleAccess => walk-up
    assert classify(_item(), _time(seats=0), False) == "full"
    assert classify(_item(), _time(seats=0, waitlist=2), False) == "waitlist_open"
    assert classify(_item(), _time(seats=3), False) == "reservable"
    assert _is_walk_up(_item(), _time()) is False


def _snap(**statuses):
    return {"sessions": {k: {"status": v, "title": k, "code": k, "scheduled_start": "2026-12-01T10:00:00", "seats_remaining": 1} for k, v in statuses.items()}}


def test_find_openings_only_on_transition_and_needs_baseline():
    prev = _snap(a="full", b="reservable", c="walk_up")
    new = _snap(a="reservable", b="reservable", c="walk_up")
    assert [o["code"] for o in find_openings(prev, new)] == ["a"]
    assert find_openings({}, new) == []
    assert find_openings(new, new) == []


def _ss(eid, hour, backup=False, score=1.0):
    start = datetime(2026, 12, 1, hour, tzinfo=timezone.utc)
    s = Session(
        event_id=eid, title=eid, description="", start_date=start.date(), start_time=None, time_zone=None,
        location=None, location_mode="physical", learning_level=None, event_type=None, partner_name=None,
        registration_url=None, learn_more_url=None, score=score,
    )
    return ScheduledSession(session=s, day="2026-12-01", start=start, end=start + timedelta(hours=1), backup=backup)


class FakeRegistrar(Registrar):
    def __init__(self, snapshots, book_result):
        super().__init__({}, "e", "p", api_cfg={})
        self._snaps = list(snapshots)
        self._book_result = book_result
        self.booked = None

    async def _status(self, schedule):
        return self._snaps.pop(0) if len(self._snaps) > 1 else self._snaps[0]

    async def _book(self, wanted):
        self.booked = wanted
        return {eid: self._book_result for eid, _ in wanted}


def _full(statuses):
    return {"sessions": {k: {"status": v, "code": k.upper()} for k, v in statuses.items()}}


def test_only_reservable_sessions_are_booked_and_verified_by_api():
    sched = [_ss("a", 8), _ss("b", 9), _ss("c", 10), _ss("d", 11)]
    before = _full({"a": "reservable", "b": "walk_up", "c": "full", "d": "enrolled"})
    after = _full({"a": "enrolled", "b": "walk_up", "c": "full", "d": "enrolled"})
    reg = FakeRegistrar([before, after], ("ok", "reserved"))
    res = {r.session.event_id: r.status for r in asyncio.run(reg.register_schedule(sched))}
    assert reg.booked == [("a", "A")]
    assert res == {"a": "registered", "b": "walk_up", "c": "full", "d": "already_registered"}


def test_click_not_confirmed_by_api_is_error_not_registered():
    sched = [_ss("a", 8)]
    snap = _full({"a": "reservable"})
    reg = FakeRegistrar([snap, snap], ("ok", "reserved"))
    assert asyncio.run(reg.register_schedule(sched))[0].status == "error"


def test_conflict_is_reported_and_never_replaces():
    sched = [_ss("a", 8)]
    snap = _full({"a": "reservable"})
    reg = FakeRegistrar([snap, snap], ("conflict", "left untouched"))
    assert asyncio.run(reg.register_schedule(sched))[0].status == "conflict"


def test_backup_used_when_primary_not_reservable():
    sched = [_ss("p", 8), _ss("bk", 8, backup=True)]
    snap = _full({"p": "full", "bk": "reservable"})
    reg = FakeRegistrar([snap, _full({"p": "full", "bk": "enrolled"})], ("ok", "reserved"))
    asyncio.run(reg.register_schedule(sched))
    assert reg.booked == [("bk", "BK")]


def test_no_browser_when_nothing_reservable():
    sched = [_ss("a", 8)]
    reg = FakeRegistrar([_full({"a": "full"})], ("ok", ""))
    asyncio.run(reg.register_schedule(sched))
    assert reg.booked is None


def test_add_events_keeps_last_20_and_appends():
    from agent.seats import add_events

    prev = {"events": [{"time": "t", "text": f"e{i}"} for i in range(20)]}
    data = {}
    add_events(data, prev, ["new1", "new2"])
    assert len(data["events"]) == 20
    assert [e["text"] for e in data["events"][-2:]] == ["new1", "new2"]
    assert data["events"][0]["text"] == "e2"


def test_only_restricts_booking_to_given_event_ids():
    sched = [_ss("a", 8), _ss("b", 9)]
    snap = _full({"a": "reservable", "b": "reservable"})
    reg = FakeRegistrar([snap, _full({"a": "enrolled", "b": "reservable"})], ("ok", "reserved"))
    asyncio.run(reg.register_schedule(sched, only={"a"}))
    assert reg.booked == [("a", "A")]


def test_platform_compat_paths_and_locks(monkeypatch, tmp_path):
    from agent import platform_compat as pc

    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc, "IS_MACOS", True)
    assert pc.tmp_dir().as_posix() == "/tmp"
    assert pc.chrome_profile1_cookies().as_posix().endswith("Library/Application Support/Google/Chrome/Profile 1/Cookies")
    assert [p.name for p in pc.profile_lock_files(tmp_path)] == ["SingletonLock", "SingletonSocket", "SingletonCookie"]

    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(pc, "IS_MACOS", False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "Google/Chrome/User Data/Profile 1/Network").mkdir(parents=True)
    (tmp_path / "Google/Chrome/User Data/Profile 1/Network/Cookies").write_text("x")
    assert pc.chrome_profile1_cookies().as_posix().endswith("Profile 1/Network/Cookies")
    assert [p.name for p in pc.profile_lock_files(tmp_path)] == ["lockfile"]


def test_notify_text_never_raises_and_uses_platform_command(monkeypatch):
    from agent import platform_compat as pc

    calls = []
    monkeypatch.setattr(pc.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(pc.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(pc, "IS_MACOS", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    pc.notify_text("it's open")
    monkeypatch.setattr(pc, "IS_MACOS", False)
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    pc.notify_text("it's open")
    assert calls[0][0] == "osascript" and calls[1][0] == "powershell" and "it''s open" in calls[1][-1]


def test_auto_reserve_runs_registrar_and_refreshes_status(monkeypatch):
    """Regression: _auto_reserve used Registrar without importing it (NameError on the first real opening)."""
    from types import SimpleNamespace

    import agent.main as main_mod
    import agent.registrar as registrar_mod
    import agent.seats as seats_mod
    from agent.registrar import RegistrationResult

    sess = SimpleNamespace(event_id="e1", title="Opened session")
    ss = SimpleNamespace(session=sess, backup=False)
    calls = {}

    class FakeRegistrar:
        def __init__(self, cfg, email, password):
            calls["init"] = (email, password)

        async def register_schedule(self, schedule, only=None):
            calls["only"] = only
            return [RegistrationResult(session=sess, status="registered", message="ok")]

    async def fake_fetch(schedule, profile_id, widget_id):
        return {"sessions": {}, "events": []}

    monkeypatch.setattr(registrar_mod, "Registrar", FakeRegistrar)
    monkeypatch.setattr(seats_mod, "fetch_seat_status", fake_fetch)
    monkeypatch.setattr(seats_mod, "write_status", lambda d: calls.setdefault("written", d))
    monkeypatch.setattr(seats_mod, "notify_text", lambda *a, **k: calls.setdefault("notified", a))

    fresh = main_mod._auto_reserve({"registration": {"email": "a@b.c"}}, [ss], [{"title": "Opened session"}], {"events": []})

    assert calls["only"] == {"e1"}
    assert calls["written"] is fresh
    assert "Reserved 1" in calls["notified"][0]


def test_auth_expiry_alert_is_rate_limited_and_rearms(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    import agent.seats as seats_mod

    monkeypatch.setattr(seats_mod, "AUTH_ALERT_STATE", tmp_path / "auth.json")
    monkeypatch.setattr(seats_mod, "ALERT_LOG", tmp_path / "alerts.log")
    monkeypatch.setattr(seats_mod, "STATUS_PATH", tmp_path / "seats.json")
    sent = []
    monkeypatch.setattr(seats_mod, "notify_text", lambda text, *a, **k: sent.append(text))
    t0 = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

    assert seats_mod.notify_auth_expired(t0) is True
    assert seats_mod.notify_auth_expired(t0 + timedelta(hours=1)) is False  # inside cooldown
    assert seats_mod.notify_auth_expired(t0 + timedelta(hours=7)) is True  # cooldown elapsed
    seats_mod.clear_auth_alert()
    assert seats_mod.notify_auth_expired(t0 + timedelta(hours=7, minutes=1)) is True  # re-armed after recovery
    assert len(sent) == 3 and "Chrome Profile 1" in sent[0]


def test_email_comes_from_env_and_is_not_written_back(monkeypatch, tmp_path):
    import agent.main as main_mod
    import agent.wizard as wiz

    cfg = tmp_path / "config.yaml"
    cfg.write_text("registration:\n  enabled: true\n  email: ''\n")
    monkeypatch.setattr(main_mod, "_CONFIG_PATH", cfg)
    monkeypatch.setattr(wiz, "_CONFIG_PATH", cfg)
    monkeypatch.setenv("AWSEVENTS_EMAIL", "me@example.com")

    assert main_mod._load_config()["registration"]["email"] == "me@example.com"
    loaded = wiz._load_config()
    assert loaded["registration"]["email"] == "me@example.com"
    wiz._save_config(loaded)
    assert "me@example.com" not in cfg.read_text()
