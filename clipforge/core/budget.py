"""A client-side throttle for a per-minute token budget.

Groq's free tier allows 8,000 tokens a minute and counts both the prompt and
the `max_tokens` reservation against it. Measured on 13 Nov 2026: one summary
window costs about 2,000 tokens, so roughly four fit in a minute — but sent
back to back they drain the bucket, and a refused request debits it too, so
blind retrying digs the hole deeper. Waiting *after* a refusal is strictly
worse than not earning one.

So the provider's own headers drive the pace. Each response says how much is
left and when the bucket refills; the next request waits for room before it is
sent rather than being refused and retried.

One process, one worker, one heavy job at a time (PRD section 9), so a plain
module-level dict is the right amount of machinery here.
"""
from __future__ import annotations

import logging
import re
import threading
import time

log = logging.getLogger("clipforge.budget")

#: Never sleep longer than this in one wait, so a stall is visible in the log
#: rather than looking like a hang.
MAX_WAIT = 70.0

#: Assume this much is needed when nothing better is known.
DEFAULT_COST = 3000

_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)m)?(\d+(?:\.\d+)?)(ms|s)\b")


def parse_duration(text: str | None) -> float | None:
    """Groq writes resets as '615ms', '12.217s' or '2m52.8s'."""
    if not text:
        return None
    match = _DURATION.fullmatch(text.strip())
    if match is None:
        return None
    minutes, value, unit = match.groups()
    seconds = float(value) / 1000 if unit == "ms" else float(value)
    return float(minutes or 0) * 60 + seconds


class Budget:
    """What one model has left, as the provider last reported it."""

    def __init__(self) -> None:
        self.limit: float | None = None
        self.remaining: float | None = None
        self.full_at: float = 0.0          # monotonic time the bucket is full

    def update(self, headers) -> None:
        try:
            got = {k.lower(): v for k, v in headers.items()}
        except Exception:
            return
        limit = got.get("x-ratelimit-limit-tokens")
        remaining = got.get("x-ratelimit-remaining-tokens")
        reset = parse_duration(got.get("x-ratelimit-reset-tokens"))
        try:
            if limit is not None:
                self.limit = float(limit)
            if remaining is not None:
                self.remaining = float(remaining)
        except ValueError:
            return
        if reset is not None:
            self.full_at = time.monotonic() + reset

    def wait_for(self, cost: float, *, sleep=None) -> float:
        """Sleep until `cost` tokens should be available. Returns seconds slept.

        Refilling is assumed linear across the reset window, which is what a
        per-minute allowance behaves like.

        `sleep` is resolved here rather than as a default argument: a default
        binds the function at import, which makes it unpatchable and silently
        turns a test into a real wait.
        """
        sleep = sleep or time.sleep
        if self.remaining is None or self.limit is None or cost <= self.remaining:
            return 0.0
        short_by = cost - self.remaining
        refill_window = max(self.full_at - time.monotonic(), 0.0)
        if refill_window <= 0 or self.limit <= self.remaining:
            # The bucket should already be full; nothing useful to wait for.
            self.remaining = self.limit
            return 0.0
        per_second = max((self.limit - self.remaining) / refill_window, 1e-6)
        needed = min(short_by / per_second, MAX_WAIT)
        log.info("budget: %.0f of %.0f tokens left, need %.0f; waiting %.1fs",
                 self.remaining, self.limit, cost, needed)
        sleep(needed)
        # Credit the wait, so a long job paces instead of sleeping twice over.
        self.remaining = min(self.limit, self.remaining + per_second * needed)
        return needed

    def spend(self, cost: float) -> None:
        """Assume a request just cost this much, pending the real headers."""
        if self.remaining is not None:
            self.remaining = max(0.0, self.remaining - cost)


_budgets: dict[str, Budget] = {}
_lock = threading.Lock()


def for_model(model: str) -> Budget:
    with _lock:
        return _budgets.setdefault(model, Budget())


def reset() -> None:
    """Forget everything. For tests."""
    with _lock:
        _budgets.clear()


def estimate_cost(messages: list[dict[str, str]], max_tokens: int) -> int:
    """Roughly what a request will be charged.

    Measured against Groq on 13 Nov 2026: 4,827 characters of prompt came to
    1,619 tokens, so about three characters a token. `max_tokens` is counted in
    full even when the answer is shorter, which is what made 16,000-token
    defaults so expensive.
    """
    chars = sum(len(m.get("content") or "") for m in messages)
    return int(chars / 3) + int(max_tokens)
