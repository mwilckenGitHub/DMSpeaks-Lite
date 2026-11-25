"""Locate a submitted player name using provider word times and decoded audio."""

import re

import numpy as np

from alignment_backend import bind_word_times
from guide_bindings import RATE
from timing_evidence import word_labels


def transcript_tokens(transcript):
    return [{"word": m[0], "first": m.start(), "last": m.end()}
            for m in re.finditer(r"[^\W_]+(?:['’][^\W_]+)*", transcript)]


def name_window(transcript, span, timed_words, sample_count):
    """Bind the returned timing labels to one exact name and decoded clock."""
    tokens = transcript_tokens(transcript)
    duration = sample_count / RATE
    # The streaming clock can place the final word's end just past the WAV.
    # Clamp this terminal estimate while still validating every word and span.
    if (timed_words and timed_words[-1]["start"] < duration
            and duration < timed_words[-1]["end"] <= duration + .04):
        timed_words = [*timed_words[:-1], {**timed_words[-1], "end": duration}]
    words = bind_word_times(timed_words, tokens, duration)
    a, b = span
    selected = [i for i, word in enumerate(words) if a <= word["first"] < word["last"] <= b]
    if not selected or words[selected[0]]["first"] != a or words[selected[-1]]["last"] != b:
        raise ValueError("The provider words do not identify the requested name occurrence.")
    first, last = selected[0], selected[-1]
    begin, end = (round(value * RATE) for value in (words[first]["start"], words[last]["end"]))
    if not 0 <= begin < end <= sample_count:
        raise ValueError("The provider did not return a positive interval for this name.")
    return words, first, last, begin, end


def extra_fallback_words(transcript, span, timed_words, sample_count):
    """Bind extra fallback labels without assigning them to a source word."""
    expected = word_labels(transcript_tokens(transcript))
    observed = word_labels(timed_words)
    if len(observed) <= len(expected):
        return []
    matched, insertions = [], []
    for index, (label, word) in enumerate(zip(observed, timed_words, strict=True)):
        position = len(matched)
        if position < len(expected) and label == expected[position]:
            matched.append(word)
        elif label == "adventurer":
            insertions.append((index, position, word))
        else:
            return []
    try:
        _, first, last, _, _ = name_window(transcript, span, matched, sample_count)
    except ValueError:
        return []
    fallback_words = []
    for index, position, word in insertions:
        start, end = round(word["start"] * RATE), round(word["end"] * RATE)
        if not 0 <= start < end <= sample_count:
            return []
        fallback_words.append(dict(index=index, position=position, samples=[start, end],
                                   near_name=first <= position <= last + 1))
    return fallback_words


def name_cut(samples, transcript, span, timed_words, *, preceding_phone=None):
    """Measure a bounded cut and retain the provider coordinates for review."""
    words, first, last, begin, end = name_window(transcript, span, timed_words, len(samples))
    terminal = last == len(words) - 1
    next_start = len(samples) if terminal else round(words[last + 1]["start"] * RATE)
    frame = round(.005 * RATE)
    quiet = 10 ** (-45 / 20)
    # A short stop closure can precede an audible release. Use the first
    # sustained quiet after the reported end, including a short decay handle.
    tail_from = max(begin + frame, end)
    tail_to = min(len(samples), next_start, end + 24 * frame)
    tail = None
    for start in range(tail_from, tail_to - 8 * frame + 1, frame):
        window = samples[start:start + 8 * frame]
        frames = window.reshape(8, frame)
        if (np.max(np.abs(frames)) <= quiet * 4
                and np.all(np.mean(frames ** 2, axis=1) <= quiet ** 2)):
            # A quiet closure followed by a stop release is still part of
            # the name. Check through EOF or up to the next timed word.
            if not quiet_through_end(samples[start + 8 * frame:next_start], frame, quiet):
                continue
            tail = start + 2 * frame
            break
    if tail is None and terminal and len(samples) - end < 8 * frame:
        # A short terminal window can be legitimate, but the timestamp
        # alone cannot distinguish quiet from trailing speech.
        quiet_start = min(end, len(samples) - 3 * frame)
        if quiet_start > begin and quiet_through_end(samples[quiet_start:], frame, quiet):
            tail = len(samples)
    if tail is None and not terminal:
        # A reported word end can land at the next onset, after a real pause.
        # Use only a sustained quiet run that reaches that boundary. A quiet
        # pocket followed by speech inside the name is not an eligible cut.
        edge = min(end, next_start)
        earliest = max(begin + frame, end - 55 * frame)
        stop = edge
        if quiet_through_end(samples[edge:next_start], frame, quiet):
            while stop - frame >= earliest:
                block = samples[stop - frame:stop]
                if np.max(np.abs(block)) > quiet * 4 or np.mean(block ** 2) > quiet ** 2:
                    break
                stop -= frame
        if edge - stop >= 8 * frame:
            tail = stop + 2 * frame
    if tail is None:
        raise ValueError("The name has no verified quiet decay in its timestamp window.")
    lead = 0 if first == 0 else begin
    if first:
        previous = round(words[first - 1]["end"] * RATE)
        if previous - begin > 4 * frame:
            raise ValueError("The provider overlaps the name with the preceding word.")
        if preceding_phone in {"CH", "JH", "S", "SH", "Z", "ZH"} and abs(previous - begin) <= 2 * frame:
            lead = after_frication(samples, begin, end) or begin
    if lead >= tail:
        raise ValueError("The name cut collapses after waveform refinement.")
    return {"provider_samples": [begin, end], "donor_samples": [lead, tail],
            "method": "provider_word_timing_and_waveform_decay"}


def quiet_through_end(samples, frame, quiet):
    complete = len(samples) // frame * frame
    if samples.size and np.max(np.abs(samples)) > quiet * 4:
        return False
    if complete and np.any(np.mean(samples[:complete].reshape(-1, frame) ** 2, axis=1) > quiet ** 2):
        return False
    remainder = samples[complete:]
    return not remainder.size or np.mean(remainder ** 2) <= quiet ** 2


def after_frication(samples, start, end):
    """Move past a sustained release still present at the reported onset."""
    hop, half = round(.004 * RATE), round(.008 * RATE)
    stop = min(end, len(samples)) - half
    if start < half or start + 5 * hop > stop:
        return None
    centers = np.arange(start, min(stop, start + 10 * hop) + 1, hop)
    windows = samples[centers[:, None] + np.arange(-half, half)]
    power = np.abs(np.fft.rfft(windows * np.hanning(2 * half), axis=1)) ** 2
    frequencies = np.fft.rfftfreq(2 * half, 1 / RATE)
    high = power[:, (frequencies >= 3500) & (frequencies <= 11000)].sum(axis=1)
    low = power[:, (frequencies >= 80) & (frequencies <= 2200)].sum(axis=1)
    ratio = 10 * np.log10(np.maximum(high, 1e-12) / np.maximum(low, 1e-12))
    if np.min(ratio[:3]) < 6 or np.mean(windows[0] ** 2) < 10 ** (-45 / 10):
        return None
    falling = np.flatnonzero((ratio[3:-1] <= 3) & (ratio[4:] <= 3))
    if falling.size:
        return int(centers[falling[0] + 3])
    return None
