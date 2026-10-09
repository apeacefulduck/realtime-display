"""One process-wide provider cooldown, shared by HTTP and WebSocket requests."""
import math
import time
from email.utils import parsedate_to_datetime


def retry_after_seconds(value):
    if not value:
        return 0
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, math.ceil(seconds)) if math.isfinite(seconds) else 0


class WeatherRetry:
    def __init__(self):
        self.reset()

    def reset(self):
        self.failures = 0
        self.until = 0
        self.error = "provider_unavailable"

    def remaining(self):
        return max(0, math.ceil(self.until - time.monotonic()))

    def fail(self, status=None, retry_after=None):
        self.failures = min(self.failures + 1, 7)
        self.error = "rate_limited" if status == 429 else "provider_unavailable"
        base, cap = (300, 3600) if status == 429 else (60, 900)
        delay = max(min(base * 2 ** (self.failures - 1), cap),
                    retry_after_seconds(retry_after))
        self.until = time.monotonic() + delay
        return delay
