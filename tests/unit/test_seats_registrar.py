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
