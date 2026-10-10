"""Cooperative wall-clock deadline shared by crawling, retries and downloads."""
import time


class RunStopped(BaseException):
    # Adapter error handlers must not turn a normal budget stop into a crawl error.
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class RunBudget:
    def __init__(self, seconds=None):
        self.started = time.monotonic()
        self.deadline = self.started + seconds if seconds else None

    def check(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RunStopped("max_seconds_reached")

    def timeout(self, default):
        self.check()
        return min(default, max(0.001, self.deadline-time.monotonic())) if self.deadline else default

    def sleep(self, seconds):
        self.check()
        seconds = max(0, seconds)
        if self.deadline is not None:
            remaining = max(0, self.deadline-time.monotonic())
            if seconds >= remaining:
                time.sleep(remaining)
                # Never continue a request after truncating its required rate wait.
                raise RunStopped("max_seconds_reached")
        time.sleep(seconds)
        self.check()
