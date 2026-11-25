"""Validate a provider return before admitting it to the personal name library."""
from io import BytesIO
from pathlib import Path
from shutil import copy2
from tempfile import TemporaryDirectory
from time import time_ns
import os
import re
import unicodedata
import wave

import numpy as np

from lite_portable_join import expanded
from lite_portable_io import read, save


class ReturnNeedsReview(ValueError):
    """One previously submitted recording needs an explicit retry decision."""


def tokens(text):
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFC", expanded(text)).casefold())


def reported_name_interval(transcript, span, words):
    """Bind the name to its position, allowing extra reported Adventurer labels.

    A forced alignment can fit an expected name to a different performance.
    Keep the provider's original reported words independent of that alignment.
    """
    start, stop = span
    before, name, after = (tokens(text) for text in (transcript[:start], transcript[start:stop], transcript[stop:]))
    expected = before + name + after
    matched = []
    for word in words:
        for label in tokens(word["word"]):
            index = len(matched)
            if index < len(expected) and expected[index] == label:
                matched.append(word)
            elif label != "adventurer":
                raise ReturnNeedsReview("The provider's words do not match the requested name in context.")
    if not name or len(matched) != len(expected):
        raise ReturnNeedsReview("The provider did not confirm the requested name in context.")
    return matched[len(before)]["start"], matched[len(before) + len(name) - 1]["end"]


def audible(samples):
    # Ignore DC offset. A single nonzero sample in an otherwise muted clip
    # is not usable speech. Work in short frames to preserve quiet names.
    frame = 220
    if len(samples) < frame:
        return False
    frames = samples[:len(samples) // frame * frame].reshape(-1, frame)
    return np.count_nonzero(np.ptp(frames.astype(np.int32), axis=1) >= 16) >= 2


def verify_audio(audio, transcript, span, words, *, timed):
    with wave.open(BytesIO(audio)) as stream:
        rate = stream.getframerate()
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")
    if not audible(samples):
        raise ReturnNeedsReview("The provider returned silent audio.")
    if timed:
        start, stop = reported_name_interval(transcript, span, words)
        # Streaming timestamps sometimes give a zero-duration estimate for a
        # short word. The subsequent phone alignment owns the precise cut.
        if stop > start and not audible(samples[max(0, round(start * rate)):round(stop * rate)]):
            raise ReturnNeedsReview("The provider returned silence where the requested name should be.")


def preserve_attempt(directory, receipt):
    """Keep the old receipt and any audio before a user-authorized resubmission."""
    archive = directory / "previous-attempts"
    archive.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=archive) as temp:
        staged = Path(temp) / "attempt"
        staged.mkdir()
        save(staged / "receipt.json", receipt)
        for name in ("return.wav", "phones.json", "fish-raw.sse", "fish-raw.wav", "provider-raw.bin", "hold.json"):
            source = directory / name
            if source.is_file():
                try:
                    os.link(source, staged / name)
                except OSError:
                    copy2(source, staged / name)
        staged.rename(archive / str(time_ns()))


def held_return(directory, receipt):
    if receipt.get("state") in {"submitting", "return_requires_inspection", "unusable_return"}:
        return True
    marker = directory / "hold.json"
    if not marker.is_file() or not receipt.get("wav_sha256"):
        return False
    hold = read(marker)
    if hold.get("wav_sha256") != receipt["wav_sha256"]:
        return False
    # The previous cut checks assumed a constrained donor interval. Re-evaluate
    # the cached performance with independent name timing before any new request.
    if (receipt.get("state") == "returned" and (directory / "return.wav").is_file()
            and "cut_review_version" not in hold and hold.get("reason") in {
                "No measured phonetic cut fits the prepared slot and preserves its neighboring speech.",
                "No measured paced Name cut preserves the existing contextual base and fallback.",
            }):
        return False
    return True


class PreviousNameReturns:
    """Look up legacy contradictory labels only for installed, locally identifiable recordings."""

    def __init__(self, queue):
        self.queue = queue
        self.by_dialogue = None

    def for_dialogue(self, source_id, provider):
        if self.by_dialogue is None:
            self.by_dialogue = {}
            for path in self.queue.glob("*/receipt.json"):
                try:
                    receipt = read(path)
                except (OSError, ValueError):
                    continue
                if (not isinstance(receipt, dict) or receipt.get("request_sha256") != path.parent.name or receipt.get("state") != "returned"
                        or receipt.get("verified_return_version") or not receipt.get("word_timing_evidence")):
                    continue
                key = receipt.get("source_id"), receipt.get("provider_id")
                self.by_dialogue.setdefault(key, []).append((path.parent, receipt))
        return self.by_dialogue.get((source_id, provider), ())

    def contradictions(self, row, name, provider):
        from lite_portable_constraints import name_occurrences
        from lite_portable_render import name_text

        wrong = []
        for directory, receipt in self.for_dialogue(row["source_id"], provider):
            evidence = receipt["word_timing_evidence"]
            groups = evidence.get("observations") if isinstance(evidence, dict) else None
            observation = groups.get("generation") if isinstance(groups, dict) else None
            if not isinstance(observation, dict):
                continue
            checksum = receipt.get("wav_sha256")
            if (not checksum or checksum != observation.get("audio_sha256")
                    or checksum != evidence.get("bound_audio_sha256")):
                continue
            for slot in name_occurrences(row):
                transcript, span = name_text(slot, name)
                if transcript != receipt.get("transcript"):
                    continue
                words = observation.get("words", [])
                if not isinstance(words, list) or not all(
                        isinstance(word, dict) and isinstance(word.get("word"), str) for word in words):
                    continue
                start, stop = span
                before, expected, after = (tokens(text) for text in
                                           (transcript[:start], transcript[start:stop], transcript[stop:]))
                observed = [label for word in words for label in tokens(word["word"])]
                # Bind the conflicting words to the actual name slot. A provider
                # can omit labels or transcribe only the beginning of a good take.
                end = len(observed) - len(after)
                if (not expected or end <= len(before) or observed[:len(before)] != before
                        or observed[end:] != after):
                    continue
                reported = observed[len(before):end]
                if all(word == "adventurer" or word in expected for word in reported):
                    continue
                if any(reported[i:i+len(expected)] == expected for i in range(len(reported))):
                    continue
                wrong.append((directory, receipt, slot.get("name_occurrence", 1)))
        return wrong
