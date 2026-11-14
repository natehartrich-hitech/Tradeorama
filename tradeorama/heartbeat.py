"""Heartbeat pings to a dead man's switch such as Healthchecks.io.

The bot pings a URL while it's running. If the pings stop (the PC is off, the bot crashed or
hung), the service alerts you. Nothing on this PC can raise that alert itself, since a PC that
is off can't send anything, so the check has to live elsewhere.

A heartbeat must never stop the bot: every network error is swallowed and reported once.
"""

import sys
import time
import urllib.request
from typing import Callable


class Heartbeat:
    def __init__(
        self,
        url: str | None,
        every_sec: float = 600,
        timeout: float = 10,
        clock: Callable[[], float] = time.monotonic,
        send: Callable[[str, bytes | None], None] | None = None,
    ):
        self.url = (url or "").strip().rstrip("/") or None
        self.every_sec = every_sec
        self.timeout = timeout
        self.clock = clock
        self._send = send or self._post
        self._last: float | None = None
        self._failing = False

    @property
    def enabled(self) -> bool:
        return self.url is not None

    def beat(self, force: bool = False) -> None:
        """Say "still running". Throttled to one ping per `every_sec` unless forced."""
        if not self.url:
            return
        now = self.clock()
        if not force and self._last is not None and now - self._last < self.every_sec:
            return
        self._last = now  # also after a failed ping, so an outage doesn't add a timeout to every loop
        self._ping(self.url, None)

    def fail(self, message: str) -> None:
        """Report a crash right away instead of waiting for the pings to go stale."""
        if self.url:
            self._ping(self.url + "/fail", message[:10000].encode("utf-8", "replace"))

    def sleep(self, seconds: float, step: float = 60) -> None:
        """time.sleep that keeps beating during long waits."""
        end = self.clock() + seconds
        while (left := end - self.clock()) > 0:
            time.sleep(min(step, left))
            self.beat()

    def _ping(self, url: str, body: bytes | None) -> None:
        try:
            self._send(url, body)
        except Exception as exc:  # noqa: BLE001 - never let monitoring take the bot down
            if not self._failing:
                print(f"heartbeat ping failed ({exc}); will keep trying", file=sys.stderr)
            self._failing = True
        else:
            self._failing = False

    def _post(self, url: str, body: bytes | None) -> None:
        req = urllib.request.Request(url, data=body, method="POST" if body else "GET",
                                     headers={"User-Agent": "tradeorama-heartbeat"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()
