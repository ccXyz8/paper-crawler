from __future__ import annotations

import time
from threading import Lock

from .models import RateLimitConfig


class RateLimiter:
    def __init__(self, config: RateLimitConfig, sleep=None):
        self.config = config
        self.sleep = sleep or time.sleep
        self._lock = Lock()
        self._last_request = 0.0
        self._window_start = time.monotonic()
        self._window_count = 0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._window_start
            if elapsed >= 60:
                self._window_start = now
                self._window_count = 0
            if self._window_count >= self.config.requests_per_minute:
                self.sleep(max(0.0, 60 - elapsed))
                self._window_start = time.monotonic()
                self._window_count = 0
            delay = self.config.min_delay_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                self.sleep(delay)
            self._last_request = time.monotonic()
            self._window_count += 1
