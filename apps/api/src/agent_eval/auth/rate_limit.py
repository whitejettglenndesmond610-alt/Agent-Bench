from collections import defaultdict, deque
from time import monotonic


class LoginRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def retry_after(self, key: str) -> int | None:
        failures = self._active_failures(key)
        if len(failures) < self.max_attempts:
            return None
        return max(1, int(self.window_seconds - (monotonic() - failures[0])))

    def record_failure(self, key: str) -> None:
        failures = self._active_failures(key)
        failures.append(monotonic())

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)

    def _active_failures(self, key: str) -> deque[float]:
        failures = self._failures[key]
        cutoff = monotonic() - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(key, None)
            failures = self._failures[key]
        return failures
