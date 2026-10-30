"""Fake threat-intel feed for the job runtime (mocks.md §3.4)."""

# invented: the feed, its transport and its counts.

import random


def fetch_indicators(lookback_hours: int, rng: random.Random) -> int:
    """Return the synced indicator count; lookback 0 is the hollow bug."""
    if lookback_hours <= 0:
        return 0
    return rng.randint(50, 500)
