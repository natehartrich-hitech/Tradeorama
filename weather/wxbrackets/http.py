"""HTTP client with retries, exponential backoff and a per-host minimum interval between requests."""

import random
import time
from urllib.parse import urlsplit

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}


class Http:
    def __init__(
        self,
        user_agent: str,
        min_interval: dict[str, float] | None = None,
        retries: int = 4,
        backoff: float = 1.0,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True)
        self.min_interval = min_interval or {}
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep
        self.clock = clock
        self._last: dict[str, float] = {}

    def _wait_turn(self, host: str) -> None:
        gap = self.min_interval.get(host, 0.0)
        last = self._last.get(host)
        if last is not None and gap > 0:
            wait = last + gap - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last[host] = self.clock()

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        for attempt in range(self.retries + 1):
            self._wait_turn(host)
            try:
                r = self.client.get(url, params=params, headers=headers)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                self.sleep(self._delay(attempt))
                continue
            if r.status_code in RETRY_STATUS and attempt < self.retries:
                retry_after = r.headers.get("Retry-After", "")
                self.sleep(float(retry_after) if retry_after.isdigit() else self._delay(attempt))
                continue
            r.raise_for_status()
            return r
        raise RuntimeError("unreachable")

    def _delay(self, attempt: int) -> float:
        return self.backoff * 2**attempt * (1 + random.random() * 0.25)

    def json(self, url: str, params: dict | None = None, headers: dict | None = None):
        return self.get(url, params, headers).json()
