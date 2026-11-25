"""Assemble only the prepared name occurrence against verified installed audio."""

from array import array
from copy import deepcopy
from pathlib import Path
import re
from shutil import copytree, which
from tempfile import TemporaryDirectory
from time import perf_counter

import numpy as np
import soundfile as sf

from guide_bindings import RATE
from phoneme_alignment import transcript_digest, validate
from phoneme_snap import returned_name_region
from provider_name_cut import extra_fallback_words, name_window
from lite_clip_bounds import boundary_method, clip_bounds
from lite_portable_companion import seed_clips
from lite_portable_constraints import leading_word_count, name_article_source, name_clip_stem
from lite_portable_inputs import reconstructed_caption
from lite_portable_io import digest, file_hash, read, save, validate_routes
from lite_portable_join import expanded, join_discontinuities, phone_candidates, splice, trim_donor
from lite_seed_joins import discontinuous, measure_edge
from lite_source_identity import native_identity,native_filters
from timing_evidence import returned_groups


class NameCutUnavailable(ValueError):
    """A returned take could not yield a verified spoken-name clip."""

    def __init__(self, reason, *, request_id=None):
        super().__init__(reason)
        self.request_id = request_id
        self.retake_attempts = 0


def checked_name_article(row):
    article = row.get("name_article")
    if article is None:
        return None
    source = name_article_source(row)
    if (source is None or any(article.get(key) != value for key, value in source.items())
            or {article["source"].casefold(), article.get("spoken_text", "").casefold()} != {"a", "an"}):
        raise ValueError("The corrected Name article does not belong to the measured leading word.")
    return article


def name_text(row, name):
    lo, hi = row["context"]
    first, last = row["slot"]
    text = row.get("request_context", row["stock_text"][lo:hi])
    before = text[:first - lo]
    after = text[last - lo:]
    # Keep signed source offsets and phoneme evidence on the original text.
    # Separately recorded fallbacks carry the article spoken in this context.
    articles = [*row.get("class_articles", []), *row.get("race_articles", [])]
    if article := checked_name_article(row):
        articles.append(article)
    for article in sorted(articles, key=lambda item: item["source_chars"], reverse=True):
        start, end = article["source_chars"]
        spoken = article["spoken_text"]
        if end <= first:
            before = before[:start - lo] + spoken + before[end - lo:]
        else:
            after = after[:start - last] + spoken + after[end - last:]
    offset = len(before) - (len(before) - len(before.lstrip()))
    transcript = (before + name + after).strip()
    return transcript, (offset, offset + len(name) + len(row.get("name_suffix", "")))


def continuing_name_context(row):
    """Include the rest of an interrupted sentence when the request ends at the name."""
    if row.get("name_slots"):
        return row
    _, end = row["slot"]
    _, stop = row["context"]
    if not re.fullmatch(r"\s*(?:\.{3}|…|[,;:—–])\s*", row["stock_text"][end:stop]):
        return row
    return following_context_for_name_retry(row)


def following_context_for_name_retry(row):
    """Keep the same source slot and give the name a spoken word on each side."""
    if row.get("name_slots"):
        return row
    text = row["stock_text"]
    lo, hi = row["context"]
    if row.get("request_context") and row["request_context"] != text[lo:hi]:
        return row
    first, last = row["slot"]
    if count(text[last:hi]):
        return row
    # Use the next full sentence, up to a fixed length. Preserve its original
    # wording and the measured neighbors of the existing cut.
    following = re.match(r"\s*[^.!?]{1,180}[.!?](?=\s|$)", text[hi:])
    if following is None or not count(following[0]):
        return row
    previous = list(re.finditer(r"[^\W_]+(?:['’][^\W_]+)*", text[lo:first]))
    before = leading_word_count(row) + 1
    start = lo + previous[-before].start() if len(previous) >= before else lo
    stop = hi + following.end()
    result = {**row, "context": [start, stop]}
    if "request_context" in row:
        result["request_context"] = text[start:stop]
    return result


def name_alignment_text(transcript, span):
    """Use spoken numbers without changing the provider request or its identity."""
    start, stop = span
    before = expanded(transcript[:start])
    value = transcript[start:stop]
    after = expanded(transcript[stop:])
    return before + value + after, (len(before), len(before) + len(value))


def count(text):
    return len(re.findall(r"[^\W_]+(?:['’][^\W_]+)*", expanded(text)))


def wav_pcm(path):
    with sf.SoundFile(path) as audio:
        frames = audio.frames
        if audio.samplerate != RATE or audio.channels != 1 or not 0 < frames <= RATE * 120:
            raise ValueError("The prepared waveform has an unsupported sample clock.")
        pcm = audio.read(frames, dtype="float32")
    if len(pcm) != frames:
        raise ValueError("The prepared waveform ended before its declared sample count.")
    if not np.isfinite(pcm).all():
        raise ValueError("The prepared waveform contains non-finite samples.")
    return pcm


def write_clip(folder, stem, samples):
    path = folder / (stem + ".ogg")
    if which("ffmpeg"):
        from audio import encode_pcm
        pcm = array("f")
        pcm.frombytes(np.ascontiguousarray(samples, dtype=np.float32).tobytes())
        encode_pcm(pcm, path, RATE)
    else:
        # Older standalone installs can generate these clips using libsndfile alone.
        with sf.SoundFile(path, mode="w", samplerate=RATE, channels=1, format="OGG", subtype="VORBIS") as output:
            for offset in range(0, len(samples), 32768):
                output.write(samples[offset:offset + 32768])
        from vorbis_pcm import measure_vorbis, single_stream_end
        if measure_vorbis(path) != (len(samples), RATE, 1) or single_stream_end(path) != len(samples):
            raise ValueError("The encoded Ogg does not contain the complete measured cut.")
    return dict(file=path.name, sha256=file_hash(path), samples=len(samples))


def fixed_parts(folder, row, base, start, end):
    key = row["key"]
    parts = []
    if start > 0:
        parts.append(dict(kind="fixed", source_samples=[0, start],
                          clip=write_clip(folder, key + "-fixed-1", base[:start])))
    fallback = write_clip(folder, key + "-name-adventurer-1", base[start:end])
    parts.append(dict(kind="name", occurrence=1, delimiter="$N", source_samples=[start, end],
                      fallback="adventurer", choices={"adventurer":fallback},
                      expected_values=["Adventurer"]))
    if end < len(base):
        parts.append(dict(kind="fixed", source_samples=[end,len(base)],
                          clip=write_clip(folder, key + "-fixed-2", base[end:])))
    return parts


def name_join_is_discontinuous(folder, sequence, name_part, candidate, *, seed_joins=None):
    center = sequence.index(name_part)
    for neighbor in (center - 1, center + 1):
        if not 0 <= neighbor < len(sequence):
            continue
        part = sequence[neighbor]
        choices = (part["clip"],) if part["kind"] == "fixed" else part["choices"].values()
        for clip in choices:
            if seed_joins is not None:
                if neighbor < center:
                    left = seed_joins.edge(folder, clip, "tail")
                    right = measure_edge(candidate, "head")
                else:
                    left = measure_edge(candidate, "tail")
                    right = seed_joins.edge(folder, clip, "head")
                if discontinuous(left, right):
                    return True
                continue
            adjacent = wav_pcm(folder / clip["file"])
            first, last = (adjacent, candidate) if neighbor < center else (candidate, adjacent)
            if join_discontinuities(np.concatenate((first, last)), (len(first),)):
                return True
    return False


def provider_generation_observation(asset, timing):
    groups = returned_groups({**asset, "word_timing_evidence": timing})
    for channel, words, origin, observation in groups:
        if (channel != "generation" or origin != 0 or observation.get("audio_sha256") != asset["sha256"]
                or observation.get("sample_rate") != RATE or observation.get("sample_count") != asset["sample_count"]):
            continue
        return words, observation
    return [], {}


def review_provider_name_timing(asset, timing, transcript, span, measured):
    words, observation = provider_generation_observation(asset, timing)
    if not words:
        return None, []
    try:
        _, _, _, first, last = name_window(transcript, span, words, asset["sample_count"])
    except ValueError:
        fallback = extra_fallback_words(transcript, span, words, asset["sample_count"])
        return None, [entry["samples"] for entry in fallback if entry["near_name"]]
    phoneme_start, phoneme_end = measured["name_samples"]
    return dict(audio_sha256=asset["sha256"], timing_source=observation.get("timing_source", "provider"),
                provider_samples=[first, last],
                provider_minus_phoneme_samples=[first - phoneme_start, last - phoneme_end]), []


def render(folder, row, base, return_path, audio_evidence, name, spoken_name, request_id, *,
           provider_id, companion=None, provider_timing=None):
    begin = perf_counter()
    if name.casefold() == "adventurer":
        raise ValueError("Adventurer is reserved for the contextual fallback.")
    if ("companion_seed" in row) != (companion is not None):
        raise ValueError("The mixed dialogue needs its verified DMSpeaks companion.")
    seed_joins = companion.joins if companion else None
    sealed_clips = seed_joins.clips if seed_joins else None
    request_text, request_span = name_text(row, spoken_name)
    transcript, span = name_alignment_text(request_text, request_span)
    returned_pcm = wav_pcm(return_path)
    asset = dict(sha256=file_hash(return_path), sample_rate=RATE, sample_count=len(returned_pcm))
    returned = validate(audio_evidence, asset, transcript)
    spoken_form = spoken_name + row.get("name_suffix", "")
    measured = returned_name_region(audio_evidence, asset, transcript, spoken_form, span)
    comparison, fallback_intervals = review_provider_name_timing(
        asset, provider_timing, request_text, request_span, measured)
    base_phones = row["base_phones"]["words"]
    stock, (lo, hi), (left, right) = row["stock_text"], row["context"], row["slot"]
    begin_index, end_index, first, last = [count(stock[:cut]) for cut in (lo, hi, left, right)]
    donor_first, donor_last = [count(transcript[:cut]) for cut in span]
    if donor_first != first - begin_index or len(returned) != end_index - last + donor_last:
        raise ValueError("The returned phones differ from the prepared dialogue context.")
    starts, ends = phone_candidates(returned_pcm, returned, 0, len(returned), donor_first, donor_last)
    trim_donor(returned_pcm, returned, starts, ends)
    donor = dict(phones=returned, samples=returned_pcm, starts=starts, ends=ends,
                 begin=begin_index, extra=donor_last - donor_first - (last - first))
    leader = row.get("leading_word")
    article = checked_name_article(row)
    article_join = {"source": article["source"], "spoken": article["spoken_text"]} if article else None
    candidates = splice(base, base_phones, donor, begin_index, end_index, first, last, row["source_cut"],
                        name_only=not leader, existing_leader=leading_word_count(row), article=article_join)
    # Keep measured speech intact when the decoded join passes. Try a taper
    # only after exhausting the eligible cuts on the original signal.
    taperable = {"connected_phonemes", "independent_name_boundary"}
    trials = [(source, take, edges, False) for _, source, take, edges in candidates]
    trials += [(source, take, edges, True) for _, source, take, edges in candidates
               if any(edge["method"] in taperable for edge in edges.values())]
    aligned = perf_counter()
    value, start_end = name.casefold(), tuple(row["source_cut"])
    occurrence = row.get("name_occurrence", 1)
    repairing_article = False

    def name_part(manifest):
        return next(part for part in manifest["sequence"]
                    if part["kind"] == "name" and part["occurrence"] == occurrence)

    if folder.is_dir():
        before = read(folder / "dialogue.json")
        if before.get("companion_seed") != row.get("companion_seed"):
            raise ValueError("The staged dialogue has a different companion binding.")
        if before.get("reference_contract") != row.get("reference_contract"):
            raise ValueError("The staged dialogue uses a different ordered voice-reference contract.")
        if before.get('native_source')!=native_identity(row):
            raise ValueError('The staged dialogue uses a different native source preparation.')
        existing = name_part(before)["choices"].get(value)
        if existing:
            if existing.get("reference_contract_sha256") != row.get("reference_contract", {}).get("sha256"):
                raise ValueError("The saved Name choice has a different ordered voice reference.")
            if (existing["donor_sha256"], existing["request_sha256"]) != (asset["sha256"], request_id):
                old_row = {key: item for key, item in row.items() if key != "name_article"}
                old_request, old_span = name_text(old_row, spoken_name)
                old_text, _ = name_alignment_text(old_request, old_span)
                repairing_article = (article is not None and not existing.get("name_article")
                                     and old_text != transcript and existing.get("provider_id") == provider_id
                                     and existing.get("measured_name", {}).get("transcript_sha256")
                                     == transcript_digest(old_text))
                if not repairing_article:
                    raise ValueError("A different take already occupies this prepared occurrence.")
            validate_routes(folder, before, folder, sealed_clips=sealed_clips)
            if not repairing_article:
                return before
    else:
        before = None
    folder.parent.mkdir(parents=True, exist_ok=True)
    excluded_fallback = False
    with TemporaryDirectory(dir=folder.parent, prefix=".name-") as temporary:
        scratch = Path(temporary)
        for index, (source, take, edges, taper) in enumerate(trials):
            if source != start_end:
                continue
            start, stop = take
            if any(start < end and beginning < stop for beginning, end in fallback_intervals):
                excluded_fallback = True
                continue
            stage = scratch / str(index)
            if before is None:
                stage.mkdir()
                from lite_source_identity import signed_binding
                manifest = dict(source_id=row["source_id"], spoken_key=row["key"], locale=row["locale"],
                                npc_id=row["npc_id"], sample_rate=RATE, source_samples=len(base),
                                source_pcm_sha256=row["pcm_sha256"],
                                original_spoken=signed_binding(row))
                for field in ("catalog_ordinal", "npc_aliases", "source_text_sha256", "quest_eligibility",
                              "reference_contract", "quest_variant"):
                    if field in row:
                        manifest[field] = deepcopy(row[field])
                if analysis := row.get("analysis_base"):
                    manifest.update(reference_spoken=manifest["original_spoken"], original_spoken=None,
                                    analysis_base_sha256=analysis["sha256"],
                                    reconstructed_caption=reconstructed_caption(row))
                if row.get('native_source'):
                    manifest.update(native_source=native_identity(row),native_route=row['native_route'],
                                    native_delivery_policy=row['native_delivery_policy'],native_character_filters=native_filters(row))
                    if caption:=reconstructed_caption(row):manifest['reconstructed_caption']=caption
                if companion is None:
                    manifest["sequence"] = fixed_parts(stage, row, base, *source)
                else:
                    manifest["companion_seed"] = row["companion_seed"]
                    seed_clips(stage, companion, manifest)
            else:
                copytree(folder, stage)
                manifest = deepcopy(before)
            part = name_part(manifest)
            candidate = returned_pcm[start:stop].copy()
            joins = {side: edge.copy() for side, edge in edges.items()}
            if taper:
                width = min(round(.003 * RATE), len(candidate) // 2)
                fade = np.linspace(0, 1, width, dtype=candidate.dtype)
                left, right = source
                if width and left and edges["lead"]["method"] in taperable:
                    candidate[:width] = candidate[:width] * fade + base[left - 1] * (1 - fade)
                    joins["lead"]["taper_samples"] = width
                if width and right < len(base) and edges["tail"]["method"] in taperable:
                    candidate[-width:] = candidate[-width:] * (1 - fade) + base[right] * fade
                    joins["tail"]["taper_samples"] = width
            stem = name_clip_stem(row["key"], value, occurrence)
            if repairing_article:
                revision = digest(request_id.encode())[:20]
                clip = write_clip(stage, f"{stem}-{revision}", candidate)
                clip["published_file"] = stem + ".ogg"
            else:
                clip = write_clip(stage, stem, candidate)
            clip.update(donor_sha256=asset["sha256"], donor_samples=[start,stop],
                        request_sha256=request_id, provider_id=provider_id, phoneme_joins=joins, measured_name=measured)
            clip["cut"] = clip_bounds(asset["sha256"], asset["sha256"], len(returned_pcm), start, stop,
                                      boundary_method(joins["lead"], start, trailing=False),
                                      boundary_method(joins["tail"], stop, trailing=True))
            if comparison is not None:
                clip["provider_name_timing"] = comparison
            if fallback_intervals:
                clip["excluded_provider_fallback_samples"] = fallback_intervals
            if "reference_contract" in row:
                clip["reference_contract_sha256"] = row["reference_contract"]["sha256"]
            if leader:
                clip["leading_word"] = leader
            if article:
                clip["name_article"] = article
                clip["leading_word"] = {**leader, "spoken_text": article["spoken_text"]}
            if value not in part["choices"]:
                part["expected_values"].append(name)
            part["choices"][value] = clip
            if name_join_is_discontinuous(stage, manifest["sequence"], part, wav_pcm(stage / clip["file"]),
                                          seed_joins=seed_joins):
                continue
            validate_routes(stage, manifest, scratch, sealed_clips=sealed_clips)
            manifest["name_processing"] = dict(alignment_seconds=round(aligned-begin,3),
                                              total_seconds=round(perf_counter()-begin,3))
            save(stage / "dialogue.json", manifest)
            if before is None:
                stage.rename(folder)
            else:
                from lite_portable_io import cache_bytes
                cache_bytes(folder / clip["file"], (stage / clip["file"]).read_bytes())
                cache_bytes(folder / "dialogue.json", (stage / "dialogue.json").read_bytes())
            return manifest
    if excluded_fallback:
        raise ValueError("The provider reported extra Adventurer speech in a proposed Name cut. No clean candidate passed.")
    raise NameCutUnavailable("The returned recording has no verified name clip with clean playback joins.",
                             request_id=request_id)
