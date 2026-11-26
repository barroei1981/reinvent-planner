import asyncio

import httpx

from agent import registrar
from agent.registrar import Registrar
from agent.registrar_api import ADD_SESSION_PATH, find_conflict, reserve

SLOT = ["2026-12-03 13:30", "2026-12-03 14:30"]


def _item(sid, date, start, end, **extra):
    return {"sessionID": sid, "date": date, "startTime": start, "endTime": end, "title": f"T-{sid}", **extra}


class FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeClient:
    """Stands in for the authenticated httpx client; `add` decides whether addSession enrols the account."""

    def __init__(self, schedule, enrol_on_add=True, add_raises=None):
        self.schedule, self.enrol_on_add, self.add_raises = schedule, enrol_on_add, add_raises
        self.calls = []

    async def post(self, url, content=""):
        path = url.split("awsevents.com", 1)[1]
        self.calls.append((path, content))
        if path == ADD_SESSION_PATH:
            if self.add_raises:
                raise self.add_raises
            if self.enrol_on_add:
                self.schedule.append(_item("S1", "2026-12-03", "13:30", "14:30"))
            return FakeResp({"responseCode": "0"})
        return FakeResp({"mySchedule": list(self.schedule)})


def run(coro):
    return asyncio.run(coro)


def test_find_conflict_overlap_adjacent_and_double_booking():
    sched = [_item("A", "2026-12-03", "13:00", "14:00")]
    assert find_conflict(sched, SLOT) == "T-A"  # 13:30-14:00 overlaps
    assert find_conflict([_item("B", "2026-12-03", "12:30", "13:30")], SLOT) is None  # back-to-back is fine
    assert find_conflict([_item("C", "2026-12-04", "13:30", "14:30")], SLOT) is None  # other day
    assert find_conflict([_item("D", "2026-12-03", "13:00", "14:00", allowDoubleBooking=True)], SLOT) is None


def test_reserve_books_via_api_and_verifies():
    client = FakeClient([])
    assert run(reserve(client, "S1", "TIME1", SLOT)) == ("ok", "reserved via API")
    _, body = next(c for c in client.calls if c[0] == ADD_SESSION_PATH)
    assert "sessionId=S1" in body and "sessionTimeId=TIME1" in body and "waitlist=false" in body


def test_reserve_never_calls_add_when_conflicting_or_already_enrolled():
    clash = FakeClient([_item("A", "2026-12-03", "13:00", "14:00")])
    assert run(reserve(clash, "S1", "TIME1", SLOT))[0] == "conflict"
    assert all(p != ADD_SESSION_PATH for p, _ in clash.calls)
    have = FakeClient([_item("S1", "2026-12-03", "13:30", "14:30")])
    assert run(reserve(have, "S1", "TIME1", SLOT))[0] == "ok"
    assert all(p != ADD_SESSION_PATH for p, _ in have.calls)


def test_reserve_unconfirmed_or_failed_call_is_error_so_browser_can_take_over():
    assert run(reserve(FakeClient([], enrol_on_add=False), "S1", "TIME1", SLOT))[0] == "error"
    assert run(reserve(FakeClient([], add_raises=httpx.ConnectError("down")), "S1", "TIME1", SLOT))[0] == "error"


def test_book_falls_back_to_browser_only_for_api_errors(monkeypatch):
    info = {
        "ok1": {"session_time_id": "T1", "slot": SLOT},
        "bad": {"session_time_id": "T2", "slot": SLOT},
        "clash": {"session_time_id": "T3", "slot": SLOT},
    }
    results = {"ok1": ("ok", "reserved via API"), "bad": ("error", "x"), "clash": ("conflict", "y")}

    async def fake_reserve(client, sid, tid, slot):
        return results[sid]

    class FakeCtx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    browser_calls = []

    async def fake_browser(self, wanted):
        browser_calls.append(wanted)
        return {e: ("ok", "browser") for e, _ in wanted}

    monkeypatch.setattr(registrar, "reserve", fake_reserve)
    monkeypatch.setattr(registrar, "make_client", lambda *a: FakeCtx())
    monkeypatch.setattr(Registrar, "_book_browser", fake_browser)
    reg = Registrar({}, email="", password="", api_cfg={})
    out = run(reg._book([("ok1", "A"), ("bad", "B"), ("clash", "C")], info))
    assert browser_calls == [[("bad", "B")]]  # conflict and API success never go to the browser
    assert out["ok1"][0] == "ok" and out["bad"] == ("ok", "browser") and out["clash"][0] == "conflict"
