"""Estimate the current phase only after comparable successful measurements."""
from collections import defaultdict, deque
from math import isfinite
from statistics import median


class PhaseTiming:
    def __init__(self):
        self.observations = defaultdict(lambda: deque(maxlen=8))

    def measure(self, phase, size, seconds):
        if type(seconds) in (int, float) and seconds > 0 and isfinite(seconds):
            self.observations[phase].append((size, seconds))

    def estimate(self, phase, size):
        samples = [seconds for previous, seconds in self.observations[phase]
                   if size / 2 <= previous <= size * 2]
        if len(samples) >= 3 and max(samples) <= 3 * min(samples):
            return round(median(samples), 3)
        return None
