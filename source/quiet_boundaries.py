"""Verify quiet boundaries using the waveform and two independent clocks."""
from dataclasses import dataclass

import numpy as np

from guide_bindings import RATE


FRAME = RATE // 200
MIN_QUIET_FRAMES = 5
QUIET_RMS = 10 ** (-45 / 20)
MAX_TAIL_DELAY = round(.20 * RATE)
ALIGNMENT_TOLERANCE = round(.06 * RATE)
RECOGNITION_TAIL_TOLERANCE = round(.015 * RATE)
HANDLE = round(.010 * RATE)


@dataclass(frozen=True)
class QuietBoundary:
    sample: int
    quiet_start: int
    quiet_end: int


def quiet_runs(pcm, lower, upper, *, minimum_frames=MIN_QUIET_FRAMES):
    """Find consecutive 5 ms frames below both the RMS and peak limits."""
    lower, upper = max(0, lower), min(len(pcm), upper)
    frame_count = (upper - lower) // FRAME
    if frame_count < minimum_frames:
        return []
    samples = np.asarray(pcm[lower:lower + frame_count * FRAME], dtype=np.float64)
    frames = samples.reshape(-1, FRAME)
    energy = np.mean(frames * frames, axis=1)
    peak = np.max(np.abs(frames), axis=1)
    quiet = (energy <= QUIET_RMS ** 2) & (peak <= QUIET_RMS * 4)
    edges = np.flatnonzero(np.diff(np.r_[False, quiet, False])).reshape(-1, 2)
    return [
        (lower + int(start) * FRAME, lower + int(end) * FRAME)
        for start, end in edges
        if end - start >= minimum_frames
    ]


def _low_step_sample(pcm, center, start, end):
    """Keep the desired pause and choose a low-amplitude nearby sample."""
    lo = max(start + 1, center - RATE // 1000)
    hi = min(end - 1, center + RATE // 1000)
    return min(
        range(lo, hi + 1),
        key=lambda sample: (abs(pcm[sample - 1]) + abs(pcm[sample]), abs(sample - center)),
    )


def recover_short_lead(pcm, preceding_ends, marker_starts):
    """Find a unique short quiet gap supported by two word clocks."""
    if len(preceding_ends) != 3 or len(marker_starts) != 2:
        return None
    if max(preceding_ends) - min(preceding_ends) > round(.12 * RATE):
        return None
    if max(marker_starts) - min(marker_starts) > round(.15 * RATE):
        return None
    first = max(preceding_ends)
    last = min(min(marker_starts), first + round(.25 * RATE))
    if not 0 <= first < last <= len(pcm):
        return None
    gaps = quiet_runs(pcm, first, last, minimum_frames=3)
    if len(gaps) != 1:
        return None
    start, end = gaps[0]
    handle = 4 * FRAME
    if start - handle < 0 or end + handle > len(pcm):
        return None
    before = np.asarray(pcm[start - handle:start], dtype=np.float64)
    after = np.asarray(pcm[end:end + handle], dtype=np.float64)
    active_rms = 10 ** (-35 / 20)
    if min(np.mean(before**2), np.mean(after**2)) < active_rms**2:
        return None
    sample = _low_step_sample(pcm, (start + end) // 2, start, end)
    return QuietBoundary(sample, start, end)


def recover_donor_decay(pcm, end, limit):
    """Follow the active end of an infill to its first measured quiet gap."""
    limit = min(limit, len(pcm), end + 12 * FRAME)
    if not FRAME <= end < limit:
        return None
    gaps = quiet_runs(pcm, end, limit)
    if not gaps:
        return None
    quiet_start, quiet_end = gaps[0]
    if quiet_start - end > 7 * FRAME:
        return None
    if quiet_start > end:
        samples = np.asarray(pcm[end-FRAME:quiet_start], dtype=np.float64)
        levels = np.sqrt(np.mean(samples.reshape(-1, FRAME)**2, axis=1))
        if levels[0] <= QUIET_RMS or np.any(levels[1:] > levels[:-1] * 1.5):
            return None
    sample = _low_step_sample(pcm, quiet_start + HANDLE, quiet_start, quiet_end)
    return QuietBoundary(sample, quiet_start, quiet_end)


def recover_donor_attack(pcm, start, limit):
    """Keep the quiet lead-in and low-level onset before a forced word start."""
    lower = max(0, limit, start - 13 * FRAME)
    if not lower < start <= len(pcm):
        return None
    gaps = quiet_runs(pcm, lower, start)
    if not gaps:
        return None
    quiet_start, quiet_end = gaps[-1]
    if start - quiet_end > 7 * FRAME:
        return None
    attack = np.asarray(pcm[quiet_end:start], dtype=np.float64)
    if attack.size and np.max(np.abs(attack)) > .12:
        return None
    sample = _low_step_sample(pcm, quiet_end - HANDLE, quiet_start, quiet_end)
    return QuietBoundary(sample, quiet_start, quiet_end)


def recover_late_quiet_tail(pcm, marker_ends, following_starts):
    """Require quiet after a late marker reading and before the next fixed word."""
    first_onset, last_onset = sorted(following_starts)
    first_end, last_end = sorted(marker_ends)
    if (last_onset - first_onset > round(.10 * RATE)
            or last_end - first_end > round(.40 * RATE)
            or not 0 <= first_end <= last_end <= first_onset < len(pcm)):
        return None
    earliest = max(first_end, last_end - round(.10 * RATE))
    eligible = [
        (start, end) for start, end in quiet_runs(pcm, earliest, first_onset)
        if end >= last_end and first_onset - end <= round(.075 * RATE)
    ]
    if len(eligible) != 1:
        return None
    start, end = eligible[0]
    sample = _low_step_sample(pcm, start + HANDLE, start, end)
    return QuietBoundary(sample, start, end)


def find_unspoken_slot(pcm, preceding_ends, following_starts, phone_events):
    """Find insertion space when both recognizers report an empty slot."""
    left, right = max(preceding_ends), min(following_starts)
    if (max(preceding_ends) - min(preceding_ends) > round(.18 * RATE)
            or max(following_starts) - min(following_starts) > round(.18 * RATE)
            or not 0 <= left < right <= len(pcm) or right - left < 10 * FRAME):
        return None
    lower = min(preceding_ends) - 2 * FRAME
    upper = max(following_starts) + 2 * FRAME
    gaps = [(start, end) for start, end in quiet_runs(pcm, lower, upper)
            if start <= left < right <= end]
    if len(gaps) != 1 or not phone_events:
        return None
    start, end = gaps[0]
    first = _low_step_sample(pcm, max(left + FRAME, start + HANDLE), start, end)
    last = _low_step_sample(pcm, min(right - FRAME, end - HANDLE), start, end)
    if (last - first < 8 * FRAME or phone_events[0].sample >= first
            or phone_events[-1].sample < last
            or any(first <= event.sample < last for event in phone_events)):
        return None
    return QuietBoundary(first, start, end), QuietBoundary(last, start, end)


def find_boundaries(pcm, aligned_span, recognized_span, neighbors):
    """Return supported edges independently. A missing edge needs other evidence.

    Neighbors are spans for the preceding and following fixed words, as
    reported by the forced aligner and by independent recognition. This
    search can correct a delayed or early clock within the recorded interval.
    """
    left, right = aligned_span
    if not 0 <= left < right <= len(pcm):
        raise ValueError("The aligned name is outside the recording.")
    if recognized_span is None:
        return None, None
    observed_left, observed_right = recognized_span
    if not 0 <= observed_left <= observed_right <= len(pcm):
        raise ValueError("The recognition span is outside the recording.")

    preceding, following = neighbors
    if preceding:
        earliest = min(preceding) - ALIGNMENT_TOLERANCE
        latest = max(left, observed_left) + FRAME
        eligible = quiet_runs(pcm, earliest, latest)
        if eligible:
            start, end = eligible[-1]
            lead = QuietBoundary(_low_step_sample(pcm, end - HANDLE, start, end), start, end)
        else:
            lead = None
    else:
        lead = QuietBoundary(0, 0, 0) if left <= ALIGNMENT_TOLERANCE else None

    if following:
        latest = max(following)
        lower = max(left, min(right, observed_right) - ALIGNMENT_TOLERANCE)
        # Confirm that a gap near the clock estimate continues for the
        # minimum run length, even when the clock lands just into that gap.
        eligible = quiet_runs(pcm, lower, latest + MIN_QUIET_FRAMES * FRAME)
        # The observed word end can fall inside its following pause. Seek
        # from the start of that quiet run, while rejecting earlier gaps
        # within a marker that Fish pronounced twice.
        expected_end = max(left, observed_right - RECOGNITION_TAIL_TOLERANCE)
        latest_marker_end = max(right, observed_right) if observed_left == observed_right else observed_right
        eligible = [
            (start, end)
            for start, end in eligible
            if end >= expected_end and start <= min(latest, latest_marker_end + MAX_TAIL_DELAY)
        ]
        if eligible:
            start, end = eligible[0]
            tail = QuietBoundary(_low_step_sample(pcm, start + HANDLE, start, end), start, end)
        else:
            tail = None
    else:
        tail = None
        lower = max(left, min(right, observed_right) - ALIGNMENT_TOLERANCE)
        eligible = quiet_runs(pcm, lower, len(pcm))
        if eligible and len(pcm) - eligible[-1][1] < FRAME:
            start, end = eligible[-1]
            if start <= max(right, observed_right) + MAX_TAIL_DELAY:
                tail = QuietBoundary(_low_step_sample(pcm, start + HANDLE, start, end), start, end)
        if tail is None and right >= len(pcm) - ALIGNMENT_TOLERANCE:
            tail = QuietBoundary(len(pcm), len(pcm), len(pcm))

    if lead and tail and lead.sample >= tail.sample:
        return None, None
    return lead, tail
