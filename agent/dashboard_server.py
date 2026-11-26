"""Loopback dashboard server: serves the temp dir (status JSON + report page) and a re-login action.

`POST /api/relogin` opens Chrome Profile 1 at the AWS Events sign-in page, so an expired session can be
renewed from the report. Everything else is the static file serving `python3 -m http.server` gave before.
Stdlib only, so the launchd job can run it with the system Python.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from agent.platform_compat import open_chrome_profile1, tmp_dir

log = logging.getLogger("awsevents.dashboard")

RELOGIN_PATH = "/api/relogin"
RELOGIN_COOLDOWN_S = 10.0
_last_relogin = 0.0
_relogin_lock = threading.Lock()


def _is_local_host(value: str, port: int) -> bool:
    return value in {f"127.0.0.1:{port}", f"localhost:{port}"}


def relogin_allowed(host: str, origin: str, requested_with: str, port: int) -> bool:
    """Only the dashboard page itself may trigger a browser launch: block cross-site and DNS-rebinding requests."""
    if requested_with != "dashboard" or not _is_local_host(host, port):
        return False
    return not origin or _is_local_host(urlsplit(origin).netloc, port)


def claim_relogin_slot(now: float | None = None) -> bool:
    """Rate-limit launches so repeated clicks (or a stuck page) can't spawn a pile of Chrome windows."""
    global _last_relogin
    now = time.monotonic() if now is None else now
    with _relogin_lock:
        if now - _last_relogin < RELOGIN_COOLDOWN_S:
            return False
        _last_relogin = now
        return True


class Handler(SimpleHTTPRequestHandler):
    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != RELOGIN_PATH:
            self._reply(404, {"error": "not found"})
            return
        port = self.server.server_port  # type: ignore[attr-defined]  # set by HTTPServer
        if not relogin_allowed(
            self.headers.get("Host", ""), self.headers.get("Origin", ""), self.headers.get("X-Requested-With", ""), port
        ):
            log.warning(json.dumps({"type": "SECURITY", "message": "relogin request rejected", "path": self.path}))
            self._reply(403, {"error": "forbidden"})
            return
        if not claim_relogin_slot():
            self._reply(429, {"error": "a sign-in window was just opened - try again in a few seconds"})
            return
        opened = open_chrome_profile1()
        log.info(json.dumps({"type": "OPERATIONAL", "message": "relogin requested", "opened": opened}))
        self._reply(200 if opened else 500, {"opened": opened})


def make_server(directory: Path, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=str(directory)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--directory", type=Path, default=tmp_dir())
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    make_server(args.directory, args.port).serve_forever()


if __name__ == "__main__":
    main()
