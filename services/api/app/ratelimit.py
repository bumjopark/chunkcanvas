"""In-memory sliding-window limits keyed by client. Good enough for a single-process demo."""
import threading
import time
from collections import deque


class SlidingWindow:
    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 50_000:
                self._prune(now)
            return True

    def full(self, key: str) -> bool:
        """Whether the next allow() would refuse, without counting this check as a hit."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            return bool(hits) and sum(1 for t in hits if t > now - self.window) >= self.limit

    def retry_after(self, key: str) -> int:
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return 1
            return max(1, int(hits[0] + self.window - time.monotonic()) + 1)

    def _prune(self, now: float) -> None:
        for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window]:
            del self._hits[k]
