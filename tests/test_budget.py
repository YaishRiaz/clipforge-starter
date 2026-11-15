"""The client-side token throttle.

Groq's free tier allows 8,000 tokens a minute and counts the `max_tokens`
reservation as well as the prompt. Measured 13 Nov 2026: one summary window
costs about 2,000 tokens, so roughly four fit in a minute -- but sent back to
back they drain the bucket, and a refused request debits it too. Waiting after
a refusal is strictly worse than not earning one, so the pace comes from the
provider's own headers.
"""
from __future__ import annotations

import pytest

from clipforge.core import budget as budget_mod


@pytest.fixture(autouse=True)
def clean():
    budget_mod.reset()
    yield
    budget_mod.reset()


@pytest.mark.parametrize("text, expected", [
    ("615ms", 0.615),
    ("12.217s", 12.217),
    ("2m52.8s", 172.8),
    ("1m0s", 60.0),
    ("", None),
    (None, None),
    ("soon", None),
])
def test_parses_the_providers_duration_format(text, expected):
    got = budget_mod.parse_duration(text)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_reads_the_headers():
    b = budget_mod.Budget()
    b.update({"x-ratelimit-limit-tokens": "8000",
              "x-ratelimit-remaining-tokens": "6371",
              "x-ratelimit-reset-tokens": "12.217s"})
    assert b.limit == 8000 and b.remaining == 6371
    assert b.full_at > 0


def test_unreadable_headers_are_ignored():
    b = budget_mod.Budget()
    b.update({"x-ratelimit-limit-tokens": "lots"})
    assert b.limit is None


def test_headers_that_throw_are_ignored():
    class Hostile:
        def items(self):
            raise RuntimeError("no")

    b = budget_mod.Budget()
    b.update(Hostile())
    assert b.remaining is None


def test_no_wait_when_there_is_room():
    b = budget_mod.Budget()
    b.update({"x-ratelimit-limit-tokens": "8000",
              "x-ratelimit-remaining-tokens": "7000",
              "x-ratelimit-reset-tokens": "10s"})
    slept: list[float] = []
    assert b.wait_for(2000, sleep=slept.append) == 0.0
    assert slept == []


def test_waits_when_the_bucket_is_short():
    b = budget_mod.Budget()
    # 1,000 left of 8,000, refilling over 60 s: about 117 tokens a second.
    b.update({"x-ratelimit-limit-tokens": "8000",
              "x-ratelimit-remaining-tokens": "1000",
              "x-ratelimit-reset-tokens": "60s"})
    slept: list[float] = []
    waited = b.wait_for(3000, sleep=slept.append)
    assert waited > 0
    assert slept and slept[0] == pytest.approx(waited)
    # Short by 2,000 at ~117/s is about 17 s, nowhere near the cap.
    assert 5 < waited < budget_mod.MAX_WAIT


def test_a_wait_is_capped():
    """A stall should be visible in the log, not look like a hang."""
    b = budget_mod.Budget()
    b.update({"x-ratelimit-limit-tokens": "8000",
              "x-ratelimit-remaining-tokens": "0",
              "x-ratelimit-reset-tokens": "600s"})
    assert b.wait_for(8000, sleep=lambda _s: None) == budget_mod.MAX_WAIT


def test_nothing_known_means_no_wait():
    """The first request of a run must not sleep on a guess."""
    assert budget_mod.Budget().wait_for(5000, sleep=lambda _s: None) == 0.0


def test_an_already_full_bucket_does_not_wait():
    b = budget_mod.Budget()
    b.update({"x-ratelimit-limit-tokens": "8000",
              "x-ratelimit-remaining-tokens": "8000",
              "x-ratelimit-reset-tokens": "0s"})
    assert b.wait_for(9999, sleep=lambda _s: None) == 0.0


def test_waiting_credits_the_refill():
    """Otherwise a long job sleeps for the same tokens twice."""
    b = budget_mod.Budget()
    b.update({"x-ratelimit-limit-tokens": "8000",
              "x-ratelimit-remaining-tokens": "1000",
              "x-ratelimit-reset-tokens": "60s"})
    b.wait_for(3000, sleep=lambda _s: None)
    assert b.remaining >= 3000


def test_spending_draws_the_bucket_down():
    b = budget_mod.Budget()
    b.update({"x-ratelimit-limit-tokens": "8000",
              "x-ratelimit-remaining-tokens": "5000",
              "x-ratelimit-reset-tokens": "30s"})
    b.spend(2000)
    assert b.remaining == 3000
    b.spend(99999)
    assert b.remaining == 0            # never negative


def test_a_budget_is_per_model():
    assert budget_mod.for_model("a") is budget_mod.for_model("a")
    assert budget_mod.for_model("a") is not budget_mod.for_model("b")


def test_cost_counts_the_max_tokens_reservation():
    """What made a 16,000-token default so expensive on a 8,000-token minute."""
    messages = [{"role": "system", "content": "x" * 3000},
                {"role": "user", "content": "y" * 1827}]
    assert budget_mod.estimate_cost(messages, 1200) == pytest.approx(1609 + 1200, abs=5)
    assert (budget_mod.estimate_cost(messages, 16000)
            > budget_mod.estimate_cost(messages, 1200) + 14000)


def test_the_measured_window_cost_is_in_the_right_region():
    """4,827 characters of prompt measured at 1,619 tokens on 13 Nov 2026."""
    messages = [{"role": "system", "content": "x" * 4827}]
    assert 1500 <= budget_mod.estimate_cost(messages, 0) <= 1700


def test_sleep_is_resolved_at_call_time_not_import():
    """A default argument binds the function and silently makes tests wait."""
    import inspect

    assert inspect.signature(budget_mod.Budget.wait_for).parameters["sleep"].default is None
