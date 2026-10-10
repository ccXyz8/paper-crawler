from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .models import ConferenceConfig
from .rate_limit import RateLimiter
from .budget import RunBudget

LOG = logging.getLogger(__name__)


class HttpClient:
    def __init__(self, user_agent: str, conference: ConferenceConfig, *, budget=None):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.config = conference
        self.budget = budget or RunBudget()
        self.limiter = RateLimiter(conference.rate_limit, self.budget.sleep)
        self._login()

    def _login(self) -> None:
        login = self.config.login
        if not login.login_url:
            return
        self.post(
            login.login_url,
            data={login.username_field: login.username, login.password_field: login.password},
        ).close()

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        import random
        attempts = self.config.rate_limit.max_retries if method.upper() in {"GET", "HEAD"} else 0
        for attempt in range(attempts + 1):
            self.budget.check()
            self.limiter.wait()
            try:
                response = self.session.request(method, url, timeout=self.budget.timeout(self.config.rate_limit.timeout_seconds), **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                self.budget.check()
                if attempt >= attempts:
                    raise
                self.budget.sleep(self.config.rate_limit.backoff_seconds * 2**attempt + random.random())
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
                delay = self.config.rate_limit.backoff_seconds * 2**attempt
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        date = parsedate_to_datetime(retry_after)
                        delay = max(delay, (date-datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
                response.close()
                self.budget.sleep(max(0, delay) + random.random())
                continue
            try:
                response.raise_for_status()
            except requests.RequestException:
                response.close()
                raise
            try:
                self.budget.check()
            except BaseException:
                response.close()
                raise
            return response
        raise RuntimeError(f"Request failed: {method} {url}")

    def close(self) -> None:
        self.session.close()

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)
