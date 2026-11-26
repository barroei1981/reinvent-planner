import json
import threading
import urllib.error
import urllib.request

import pytest

from agent import dashboard_server as ds


def test_relogin_allowed_requires_local_host_and_marker():
    assert ds.relogin_allowed("127.0.0.1:8000", "", "dashboard", 8000)
    assert ds.relogin_allowed("localhost:8000", "http://localhost:8000", "dashboard", 8000)
    assert not ds.relogin_allowed("127.0.0.1:8000", "", "", 8000)  # no marker header
    assert not ds.relogin_allowed("evil.example:8000", "", "dashboard", 8000)  # DNS rebinding
    assert not ds.relogin_allowed("127.0.0.1:8000", "https://evil.example", "dashboard", 8000)  # cross-site


def test_claim_relogin_slot_rate_limits():
    ds._last_relogin = 0.0
    assert ds.claim_relogin_slot(now=100.0)
    assert not ds.claim_relogin_slot(now=100.0 + ds.RELOGIN_COOLDOWN_S - 1)
    assert ds.claim_relogin_slot(now=100.0 + ds.RELOGIN_COOLDOWN_S + 1)


@pytest.fixture
def server(tmp_path, monkeypatch):
    (tmp_path / "page.html").write_text("hello")
    opened = []
    monkeypatch.setattr(ds, "open_chrome_profile1", lambda *a: opened.append(a) or True)
    ds._last_relogin = 0.0
    srv = ds.make_server(tmp_path, 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port, opened
    srv.shutdown()
    srv.server_close()


def _post(port, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{ds.RELOGIN_PATH}", method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_serves_static_and_relogin_opens_chrome(server):
    port, opened = server
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/page.html", timeout=5) as r:
        assert r.read() == b"hello"
    assert _post(port, {"X-Requested-With": "dashboard"}) == (200, {"opened": True})
    assert len(opened) == 1
    assert _post(port, {"X-Requested-With": "dashboard"})[0] == 429  # cooldown
    assert len(opened) == 1


def test_relogin_rejected_without_marker_or_with_foreign_origin(server):
    port, opened = server
    assert _post(port, {})[0] == 403
    assert _post(port, {"X-Requested-With": "dashboard", "Origin": "https://evil.example"})[0] == 403
    assert opened == []
