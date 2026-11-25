"""Prepare a whole leading phrase for a sealed expanded Name occurrence."""
from hashlib import sha256
from pathlib import Path

import numpy as np
import soundfile as sf

from guide_bindings import RATE
from lite_clip_bounds import clip_bounds
from lite_delivery_unit import prepare_unit
from lite_portable_io import file_hash, hold
from lite_portable_join import phone_candidates, trim_donor
from lite_portable_render import name_text, provider_generation_observation
import phoneme_alignment
from phoneme_alignment import UNKNOWN_PHONES, lexical_text, validate
from phoneme_snap import returned_name_region, terminal_quiet_run
from provider_name_cut import extra_fallback_words, name_window, transcript_tokens
from timing_evidence import word_labels


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def returned_request(returned, receipt, bound, row, spoken_name, expected_request, provider, *,
                     request_references=None):
    """Require the exact request receipt, source, voice and spoken form."""
    transcript, name_chars = name_text(row, spoken_name)
    require(lexical_text(spoken_name) and lexical_text(spoken_name) != "adventurer",
            "The known fallback cannot be installed as a freshly returned name")
    require(receipt.get("state") == "returned"
            and receipt.get("request_sha256") == expected_request
            and receipt.get("provider_id") == provider
            and receipt.get("source_id") == row["source_id"] == bound["source_id"]
            and row["npc_id"] == bound["npc_id"]
            and receipt.get("transcript") == transcript,
            "The returned Name does not belong to the current request and source dialogue")
    require(receipt.get("reference") == {
        "reference_contract": {"sha256": bound["reference_contract_sha256"]},
        "reference": bound["ordered_reference_evidence"] if request_references is None else request_references,
    }, "The returned Name uses different ordered voice references")
    require(file_hash(Path(returned)) == receipt.get("wav_sha256"),
            "The complete returned WAV differs from its receipt")
    return transcript, name_chars


def cut(unit, transcript, spoken_name, name_chars, fixed_prefix_words, *, trailing=None):
    """Keep all owned fixed words and the entire Name through its measured release."""
    identity = dict(sha256=unit["derived_sha256"], sample_rate=RATE, sample_count=unit["derived_samples"])
    require(file_hash(unit["audio"]) == identity["sha256"], "The paced waveform changed")
    phones = unit["phones"] if trailing is None else trailing["phones"]
    aligned_text = transcript if trailing is None else trailing["transcript"]
    region = returned_name_region(phones, identity, aligned_text, spoken_name, name_chars)
    words = phones["words"]
    if trailing is not None:
        extra = words[-trailing["count"]:]
        require(all(lexical_text(word["word"]) == "adventurer" for word in extra),
                "The paced alignment does not identify the trailing Adventurer words")
        words = words[:-trailing["count"]]
        require(region["next_word_start_sample"] == extra[0]["start_sample"],
                "The paced fallback does not follow the complete expanded Name")
    first_name = next(i for i, word in enumerate(words) if word["char_start"] >= name_chars[0])
    require(first_name > 0
            and [lexical_text(word["word"]) for word in words[:first_name]]
            == list(map(lexical_text, fixed_prefix_words))
            and (trailing is not None or region["next_word_start_sample"] is None),
            "The return must contain the complete fixed prefix followed by the Name")
    require(all(word["phonemes"] and all(phone["phone"] not in UNKNOWN_PHONES for phone in word["phonemes"])
                for word in phones["words"]), "A word in the expanded Name has unresolved phonemes")
    pcm, rate = sf.read(unit["audio"], dtype="float32")
    require(rate == RATE and pcm.ndim == 1 and len(pcm) == identity["sample_count"]
            and np.isfinite(pcm).all(), "The full paced return does not match the measured sample clock")
    if trailing is not None:
        pcm = pcm[:extra[0]["start_sample"]]
    starts, ends = phone_candidates(pcm, words, 0, len(words), first_name, len(words))
    trim_donor(pcm, words, starts, ends)
    a, b = starts[0], ends[len(words)]
    quiet = terminal_quiet_run(pcm, words[-1]["phonemes"][-1])
    require(quiet is not None and b is not None and quiet[0] <= b < quiet[1],
            "The complete phrase has no measured quiet after its final Name")
    require(0 <= a <= words[0]["phonemes"][0]["start_sample"] < region["name_samples"][0]
            < region["name_samples"][1] <= b <= len(pcm),
            "The measured cut would truncate part of the prefix or Name")
    proof = dict(
        request_transcript_sha256=sha256(transcript.encode()).hexdigest(), returned_derived_wav_sha256=identity["sha256"],
        donor_name_samples=region["name_samples"], donor_context_word_indices=[0, len(words)],
        donor_context_samples=[a, b], terminal_quiet_samples=list(quiet),
        donor_context_words=[word["word"] for word in words])
    if trailing is not None:
        proof["fallback_exclusion"] = dict(provider_samples=trailing["provider_samples"],
            paced_samples=[[word["start_sample"], word["end_sample"]] for word in extra],
            alignment_sha256=trailing["alignment_sha256"])
    return pcm[a:b].copy(), proof


def trailing_fallbacks(raw_audio, receipt, transcript, span):
    """Reject unbound or internal fallbacks before measuring the owned phrase."""
    info = sf.info(raw_audio)
    asset = dict(sha256=receipt["wav_sha256"], sample_rate=RATE, sample_count=info.frames)
    observed, _ = provider_generation_observation(asset, receipt.get("word_timing_evidence"))
    expected = transcript_tokens(transcript)
    expected_count = word_labels(expected).count("adventurer")
    extra_count = word_labels(observed).count("adventurer") - expected_count
    if extra_count <= 0:
        if observed and expected_count:
            try:
                name_window(transcript, span, observed, asset["sample_count"])
            except ValueError as error:
                raise ValueError("The provider timing does not bind the intended Adventurer word") from error
        return []
    extras = extra_fallback_words(transcript, span, observed, asset["sample_count"])
    require(len(extras) == extra_count, "The provider reported an unbound Adventurer word in this return")
    require(all(item["position"] == len(expected) for item in extras),
            "The provider returned extra Adventurer speech inside or before the expanded Name phrase")
    return extras


def measure_trailing_fallbacks(unit, transcript, extras, align):
    expanded = transcript + " Adventurer" * len(extras)
    asset = dict(sha256=unit["derived_sha256"], sample_rate=RATE, sample_count=unit["derived_samples"])
    folder = Path(unit["audio"]).parent
    with hold(folder, ".pace-unit.lock"):
        path = phoneme_alignment.evidence_path(folder, asset, expanded)
        if path.is_file():
            phones = phoneme_alignment.read(folder, asset, expanded)
        else:
            phones = align(unit["audio"], asset, expanded)
            validate(phones, asset, expanded)
            phoneme_alignment.write(folder, asset, expanded, phones)
        checksum = file_hash(path)
    return dict(phones=phones, transcript=expanded, count=len(extras), alignment_sha256=checksum,
                provider_samples=[item["samples"] for item in extras])


def prepare_named_cut(raw_audio, receipt, bound, row, spoken_name, request, provider,
                      native_phones, cache, align, *, request_references=None):
    text, span = returned_request(raw_audio, receipt, bound, row, spoken_name, request, provider,
                                  request_references=request_references)
    extras = trailing_fallbacks(raw_audio, receipt, text, span)
    unit = prepare_unit(raw_audio, text, native_phones, cache, align)
    trailing = measure_trailing_fallbacks(unit, text, extras, align) if extras else None
    pcm, evidence = cut(unit, text, spoken_name, span, bound["fixed_prefix_words"], trailing=trailing)
    raw_sha = receipt["wav_sha256"]
    evidence["cut"] = expanded_bounds(raw_sha, unit, evidence)
    if not extras:
        # The two waveforms have separate phoneme measurements and silence runs.
        # An alignment of the requested words alone cannot locate a trailing fallback.
        native = dict(audio=Path(raw_audio), derived_sha256=raw_sha,
                      derived_samples=unit["raw_samples"], phones=native_phones)
        try:
            _, original = cut(native, text, spoken_name, span, bound["fixed_prefix_words"])
        except ValueError:
            pass  # The paced cut remains verified even when the raw release is ambiguous.
        else:
            evidence["native_cut"] = expanded_bounds(raw_sha, native, original)
    evidence.update(paced_unit_sha256=unit["key"], derived_phones_sha256=unit["derived_phonemes_sha256"])
    return pcm, evidence


def expanded_bounds(raw_sha, unit, proof):
    start, stop = proof["donor_context_samples"]
    return clip_bounds(raw_sha, unit["derived_sha256"], unit["derived_samples"], start, stop,
                       "initial_quiet_before_first_phoneme" if start else "recording_start",
                       "terminal_quiet_after_final_phoneme")


def smooth_edges(owned_pcm, base_pcm, sealed_span):
    """Apply the existing three-millisecond join entirely inside the returned clip."""
    start, end = sealed_span
    width = round(RATE * .003)
    require(len(owned_pcm) > 2 * width and 0 <= start < end <= len(base_pcm), "Invalid sealed source edge")
    before = base_pcm[start - 1] if start else 0
    after = base_pcm[end] if end < len(base_pcm) else 0
    ramp = np.linspace(0, 1, width, dtype=np.float32)
    result = owned_pcm.copy()
    result[:width] = before * (1 - ramp) + result[:width] * ramp
    result[-width:] = after * (1 - ramp[::-1]) + result[-width:] * ramp[::-1]
    return result, sha256(np.ascontiguousarray(result, dtype="<f4").tobytes()).hexdigest()
