"""Standalone name processing from the verified input and installed Spoken media."""

from pathlib import Path
from collections import Counter
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import timezone
from email.utils import parsedate_to_datetime
import logging
from math import isfinite
import re
import struct
from shutil import copytree
from subprocess import SubprocessError
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic, perf_counter, time, time_ns, sleep
from urllib.error import HTTPError
from urllib.request import build_opener
import wave

from audio_return_cache import finish_return, load_return, record_return, recover_return
from guide_bindings import RATE
from http_protocols import parse_response
from leading_word import WORD, indefinite_article
from lite_alignment import alignment_profile
from lite_name_automatic import addons_folder
from lite_name_bridge import dialogue_stage, fetch_summary, publish as publish_snapshot
from lite_name_providers import NameProvider, UnusableProviderAudio
from lite_shared_fish import SharedFishBusy
from lite_name_settings import DEFAULTS, PREFS, preferences
from lite_name_timing import PhaseTiming
from lite_return_review import PreviousNameReturns, ReturnNeedsReview, held_return, preserve_attempt, verify_audio
from lite_portable_companion import installed_companion
from lite_portable_constraints import (
    OPENING_NPCS, decode_return, load_name_areas, load_npc_factions, matches_character, name_occurrences,
    ordered_area_dialogues, queue_areas,
    starting_greeting_area, validate_name,
)
from lite_portable_inputs import (
    SETTING, MissingStockRecording, imported, installed, installed_plans, reconstructed_caption, render_inputs,
)
from lite_portable_io import (cache_bytes, digest, file_hash, filesystem_path, hold, read, render_fingerprint,
                              save, stock_binding, validate_routes)
from lite_portable_package import ADDON, ARCHIVE, disable_exported_routes, disable_installed_routes, install_names, package_names
from lite_portable_render import NameCutUnavailable, continuing_name_context, expanded, following_context_for_name_retry, name_article_source, name_text, render
from lite_portable_references import (LEGACY_MISSING_SPOKEN, OriginalSpokenNotCatalogued, hosted_stock_reference, reference_evidence,
                                      require_hosted_playback_base, signed_source_needed, verified_references)
from lite_portable_upgrade import carry_compatible, dialogue_folder, matches_prepared_dialogue
from lite_source_identity import source_key,signed_binding,manifest_key,native_identity
from phoneme_alignment import transcript_digest
from pocketsphinx_phonemes import PhonemeAlignmentBusy, align
from providers import _NoCheckRedirects
from timing_evidence import capture


RESUMABLE_REJECTIONS = {401, 402, 403, 429}
MAX_NAME_RETAKES = 3
MIN_REQUEST_GAP = 1.0
_request_lock = Lock()
_last_request_start = None


@contextmanager
def speech_request_slot(should_stop=None, on_wait=None):
    """Serialize provider calls and leave at least a second between starts."""
    while not _request_lock.acquire(timeout=.1):
        if should_stop and should_stop():
            yield False
            return
    try:
        remaining = (_last_request_start + MIN_REQUEST_GAP - perf_counter()
                     if _last_request_start is not None else 0)
        if remaining > 0 and on_wait:
            on_wait()
        while remaining > 0:
            if should_stop and should_stop():
                yield False
                return
            sleep(min(.1, remaining))
            remaining = _last_request_start + MIN_REQUEST_GAP - perf_counter()
        yield not (should_stop and should_stop())
    finally:
        _request_lock.release()


def start_speech_request():
    global _last_request_start
    _last_request_start = perf_counter()
    return _last_request_start


class ProviderRejected(ValueError):
    def __init__(self, http_status, retry_at=None):
        self.http_status = http_status
        self.retry_at = retry_at
        if http_status == 429:
            detail = "The provider rate limit was reached. Fetch again when the limit clears."
        else:
            detail = f"The provider declined the request (HTTP {http_status}). Check your account and fetch again."
        super().__init__(detail)


class InvalidNameTake(NameCutUnavailable):
    """A known provider response can be retried with the same original reference and text."""


# Earlier clients wrote these post-response validation failures under the same
# state as a lost connection. Only these known validation outcomes can be retried.
LEGACY_INVALID_AUDIO_REASONS = frozenset({
    "Empty audio",
    "The provider returned silent audio.",
    "The provider returned silence where the requested name should be.",
    "The provider did not confirm the requested name in context.",
    "The provider's words do not match the requested name in context.",
    "The return must be 44.1 kHz, mono and 16-bit PCM.",
    "Fish returned incomplete audio or an unexpected duration.",
})


def provider_retry_at(error):
    """Use a definite provider delay to distinguish temporary throttling."""
    value = error.headers.get("Retry-After") if error.code == 429 and error.headers else None
    if not value:
        return None
    now = time()
    try:
        value = value.strip()
        if re.fullmatch(r"[0-9]+", value):
            delay = int(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - now
        return now + max(MIN_REQUEST_GAP, delay)
    except (ValueError, OverflowError, OSError):
        return None


def archive_for(app):
    archive = app.folder / "spoken-player-names" / ARCHIVE
    if not archive.is_file():
        raise ValueError("The player-name package is unavailable.")
    return archive


def prepare_alignment(app, transcript, scratch, row):
    config = alignment_profile(app.connections.parser())
    lexicon = Path(config["dictionary"]).expanduser()
    content = lexicon.read_text(encoding="utf-8")
    known = {line.split()[0].split("(")[0].casefold() for line in content.splitlines() if line.split()}
    required = set(re.findall(r"[^\W\d_]+(?:['â€™][^\W\d_]+)*", transcript.casefold()))
    unknown = required - known
    if unknown:
        supplied = row.get("pronunciations", {})
        unsupported = sorted(unknown - supplied.keys())
        if unsupported:
            from lite_portable_pronunciation import Pronouncer

            try:
                pronouncer = Pronouncer()
            except (OSError, RuntimeError, ValueError) as error:
                raise ValueError(f"Configure eSpeak NG for words outside the dictionary, including {unsupported[0]}.") from error
        else:
            pronouncer = None
        words = []
        for word in sorted(unknown):
            phones = supplied[word] if word in supplied else pronouncer.pronunciation(word)
            if not phones:
                raise ValueError(f"A pronunciation is needed for {word}.")
            words.append(f"{word} {phones}")
        revised = scratch / "context.dict"
        revised.write_text(content.rstrip() + "\n" + "\n".join(words) + "\n", encoding="utf-8")
        config["dictionary"] = str(revised)
    return config


def resolve_name_article(app, row, spoken):
    source = name_article_source(row)
    if source is None:
        return row
    word = WORD.search(spoken).group().casefold()
    config = alignment_profile(app.connections.parser())
    dictionary = Path(config["dictionary"]).expanduser()
    onsets = set()
    for entry in dictionary.read_text(encoding="utf-8").splitlines():
        fields = entry.split()
        if len(fields) > 1 and fields[0].split("(")[0].casefold() == word:
            onsets.add(fields[1])
    if not onsets:
        phones = row.get("pronunciations", {}).get(word)
        if not phones:
            from lite_portable_pronunciation import Pronouncer
            try:
                phones = Pronouncer().pronunciation(word)
            except (OSError, RuntimeError, ValueError):
                raise ValueError(f"The Name needs a pronunciation for {word} before choosing its article.") from None
        onsets.update(phones.split()[:1] if phones else ())
    article = indefinite_article(source["source"], onsets)
    return ({**row, "name_article": {**source, "spoken_text": article}}
            if article != source["source"] else row)


def make_alignment(audio, transcript, config):
    with wave.open(str(audio), "rb") as file:
        identity = dict(sha256=file_hash(audio), sample_rate=RATE, sample_count=file.getnframes())
    return align(audio, identity, transcript, config)


def saved_match(rows, row, name, transcript, provider_id):
    expected_reference = row.get("request_reference_evidence", row.get("archived_request_references"))
    contract = row.get("reference_contract")

    def admitted_contract(item):
        if not contract:
            return item.get("reference_contract") is None
        if item.get("reference_contract") == contract:
            return True
        # The two original paired-request inputs retain independently verified
        # exact payload/reference receipts when the portable policy is added.
        return (item.get("reference_contract") is None
                and expected_reference is not None
                and row.get("archived_request_references") == expected_reference
                and item["reference_evidence"] == expected_reference)

    candidates = [item for item in rows if (item["source_id"], item["name"], item["submitted_text"], item["provider_id"],
                                       item.get("name_occurrence")) ==
               (row["source_id"], name, transcript, provider_id, row.get("name_occurrence"))]
    if row.get('native_source'):
        matches=[item for item in candidates if item.get('native_source')==native_identity(row)
                 and item.get('source_pcm_sha256')==row['pcm_sha256']
                 and item.get('reference_contract')==row.get('reference_contract')
                 and item.get('reference_evidence')==expected_reference]
        if len(matches)>1:raise ValueError('The native source has more than one saved take for this occurrence.')
        if not matches:
            if candidates:raise ValueError('The saved return does not match the exact native source preparation.')
            return None
        if matches[0]['spoken_transcript']!=transcript:
            raise ValueError('The saved native return has a different spoken transcript.')
        return matches[0]
    matches = [item for item in candidates
               if (item["stock_sha256"], item["stock_pcm_sha256"], item["reference_evidence"],
                   item.get("stock_path", row["stock_path"])) ==
                  (row["stock_sha256"], row["pcm_sha256"], expected_reference, row["stock_path"])
               and admitted_contract(item)]
    if len(matches) > 1:
        raise ValueError("The input has more than one saved take for this name occurrence.")
    if not matches:
        if candidates:
            raise ValueError("The saved Fish return does not match the prepared dialogue and voice references.")
        return None
    item = matches[0]
    if (item["stock_sha256"] != row["stock_sha256"] or item["stock_pcm_sha256"] != row["pcm_sha256"]
            or item["reference_evidence"] != expected_reference
            or item["spoken_transcript"] != transcript):
        raise ValueError("The saved Fish return does not match the prepared dialogue and voice references.")
    return item


def recent_choice(folder, row, name, transcript, provider, *, sealed_clips=None):
    path = folder / "dialogue.json"
    if not path.is_file():
        return None
    manifest = read(path)
    if stock_binding(manifest) != signed_binding(row):
        raise ValueError("A previous package references a different stock release.")
    if manifest.get('native_source')!=native_identity(row):
        raise ValueError('A previous package references a different native preparation plan.')
    if (manifest.get("analysis_base_sha256") != row.get("analysis_base", {}).get("sha256")
            or manifest.get("source_pcm_sha256") != row["pcm_sha256"]):
        raise ValueError("A previous package references a different analysis recording.")
    if manifest.get("companion_seed") != row.get("companion_seed"):
        raise ValueError("A previous package references a different DMSpeaks companion.")
    if manifest.get("reference_contract") != row.get("reference_contract"):
        raise ValueError("A previous package uses a different ordered voice-reference contract.")
    if not matches_prepared_dialogue(manifest, row):
        raise ValueError("A previous package belongs to a different prepared dialogue.")
    entry = next(p for p in manifest["sequence"] if p["kind"] == "name"
                 and p["occurrence"] == row.get("name_occurrence", 1))
    chosen = entry["choices"].get(name.casefold())
    if (chosen and chosen.get("provider_id") == provider
            and chosen.get("reference_contract_sha256") == row.get("reference_contract", {}).get("sha256")
            and chosen["measured_name"]["transcript_sha256"] == transcript_digest(expanded(transcript))):
        # Metadata alone cannot establish that the cached playback is ready.
        # Check every route before discarding any recoverable provider return,
        # including other occurrences and the declared fallback choices.
        try:
            validate_routes(folder, manifest, folder, sealed_clips=sealed_clips)
        except (OSError, ValueError) as error:
            raise ValueError("The cached dialogue audio needs recovery. Its saved return has been preserved; "
                             "no new provider request was sent. " + str(error)) from error
        return chosen
    return None


def discard_pending_return(queue, request_id):
    if not re.fullmatch(r"[0-9a-f]{64}", request_id):
        return
    history = queue / request_id
    receipt_path = history / "receipt.json"
    if not receipt_path.is_file():
        return
    with hold(history):
        finish_return(history, read(receipt_path))


def check_provider_audio(profile, raw, transcript, name_span):
    try:
        words = []
        if profile.timed:
            wav, words = parse_response("fish", raw, timestamps=True)
        elif profile.engine in {"fish", "fish_omni"}:
            wav = raw
        else:
            wav = profile.normalized_audio(raw)
        audio, frames = decode_return(wav)
        if name_span is not None:
            verify_audio(audio, transcript, name_span, words, timed=profile.timed)
        timing = None
        if words:
            asset = dict(sha256=digest(audio), sample_rate=RATE, sample_count=frames)
            timing = capture(asset, words, "provider", {"engine": "fish"})
        return audio, timing
    except (EOFError, struct.error, wave.Error) as error:
        raise UnusableProviderAudio("The provider returned an unreadable or incomplete WAV.") from error
    except (ValueError, OverflowError) as error:
        raise UnusableProviderAudio(str(error) or "The provider returned an incomplete recording.") from error


def obtain_fresh(app, row, transcript, profile, stock, queue, scratch, keep, resume,
                 on_start, on_alignment, *, cached_only, prepared_references=None,
                 hosted_references=None,
                 should_stop=None, on_wait=None, on_alignment_wait=None, verify_cached=None,
                 alignment_text=None, on_pacing_wait=None, before_submit=None,
                 name_span=None, retry_held=False, retry_missing=False, on_receipt=None, replace_saved=False,
                 resume_interrupted_fetch=False, on_rate_limit_wait=None, retake_request=None,
                 retry_uncertain=False):
    measured_text = transcript if alignment_text is None else alignment_text
    transcript_reference = row.get("analysis_base", {}).get("reference", {}).get("transcript", row["stock_text"])
    expected = row.get("request_reference_evidence")
    stock_fish = profile.engine == "fish" or (profile.engine == "fish_omni" and hosted_references is not None)
    if stock_fish:
        if not hosted_references or len(hosted_references) not in (1, 2):
            raise ValueError("Fish Audio needs the verified voice reference followed by any available Spoken recording.")
        reference = hosted_references
        selected = reference_evidence(reference)
        if row.get("native_source") and selected != expected:
            raise ValueError("The selected default voice differs from this dialogue's reference contract.")
        expected = selected
    elif "reference_contract" in row:
        if prepared_references is None:
            raise ValueError("Load the prepared ordered voice references before fetching this dialogue.")
        reference = prepared_references
    else:
        reference = dict(id=row["source_id"], path=str(stock), transcript=transcript_reference, sha256=file_hash(stock))
    accent = row.get("fish_accent_prefix")
    submitted = accent + transcript if accent and profile.engine == "fish" else transcript
    wire = profile.request(submitted, row["npc_id"], reference)
    bound = wire.get("reference_evidence")
    if stock_fish and bound != expected:
        raise ValueError("The Fish request differs from the verified voice recording or transcript.")
    if bound is None:
        bound = expected if expected is not None else {key: reference[key] for key in ("id", "sha256")}
    if "reference_contract" in row:
        if bound != expected:
            raise ValueError("The provider request changed the prepared voice-reference order or identity.")
        bound = dict(reference_contract=row["reference_contract"], reference=bound)
    if row.get('native_source'):
        bound=dict(native_source=native_identity(row),reference=bound)
    if "name_occurrence" in row:
        bound = dict(reference=bound, name_occurrence=row["name_occurrence"], source_cut=row["source_cut"])
    ident = profile.identity(wire, bound)
    history = queue / ident
    receipt_path = history / "receipt.json"
    returned = scratch / "return.wav"
    keep_studio_exchange = keep and not getattr(app, "store_managed", False)
    if (cached_only or (retry_held and not retry_missing)) and not receipt_path.is_file():
        return None
    history.mkdir(parents=True, exist_ok=True)
    provider_timing = None
    with hold(history):
        rejected_receipt = None
        previous_attempt = None
        if on_receipt:
            on_receipt(ident)
        if receipt_path.is_file():
            receipt = recover_return(history, read(receipt_path))
            if (receipt.get("state") == "return_requires_inspection" and receipt.get("request_sha256") == ident
                    and receipt.get("provider_id") == profile.ident and receipt.get("source_id") == row["source_id"]
                    and receipt.get("reason") in LEGACY_INVALID_AUDIO_REASONS):
                receipt = {**receipt, "state": "unusable_return"}
            if (receipt.get("state") in {"submitting", "return_requires_inspection"}
                    and (retake_request is not None or (retry_missing and not retry_uncertain))):
                raise ReturnNeedsReview("The earlier submission has an uncertain result. Review it before retrying.")
            if (receipt.get("state") == "provider_rejected" and receipt.get("http_status") == 429
                    and resume and on_rate_limit_wait):
                rejected_receipt = receipt
            elif (not cached_only and ident == retake_request
                  and receipt.get("state") in {"unusable_return", "returned"}):
                previous_attempt = receipt
            elif (replace_saved or ident == retake_request) and not resume_interrupted_fetch:
                previous_attempt = receipt
            elif receipt.get("state") == "unusable_return" and not cached_only and resume_interrupted_fetch:
                raise InvalidNameTake(receipt.get("reason") or "The last recording was unusable.", request_id=ident)
            elif held_return(history, receipt):
                if resume_interrupted_fetch or not retry_held or cached_only:
                    raise ReturnNeedsReview("The previous request is held. Review it before retrying.")
                previous_attempt = receipt
            cached = None if previous_attempt else load_return(history, receipt)
            if cached is not None and name_span is not None:
                from lite_portable_render import provider_generation_observation
                import io
                with wave.open(io.BytesIO(cached)) as wav:
                    asset = dict(sha256=digest(cached), sample_rate=RATE, sample_count=wav.getnframes())
                labels, _ = provider_generation_observation(asset, receipt.get("word_timing_evidence"))
                try:
                    verify_audio(cached, transcript, name_span, labels, timed=profile.timed)
                except ReturnNeedsReview as error:
                    save(history / "hold.json", dict(wav_sha256=asset["sha256"], reason=str(error)))
                    if resume_interrupted_fetch or not retry_held or cached_only:
                        raise
                    previous_attempt, cached = receipt, None
            if cached is not None:
                if verify_cached:
                    verify_cached(history / "return.wav", receipt, ident)
                cache_bytes(returned, cached)
                provider_timing = receipt.get("word_timing_evidence")
            elif previous_attempt is not None:
                pass  # Archived under the same request lock immediately before sending.
            elif cached_only:
                return None
            elif (receipt["state"] == "provider_rejected"
                  and receipt.get("http_status") in RESUMABLE_REJECTIONS):
                if not resume:
                    raise ProviderRejected(receipt["http_status"])
                rejected_receipt = receipt
            else:
                raise ValueError("This request already has a receipt. Review it before any resubmission.")
        if cached_only and not returned.is_file():
            return None
        configuration = prepare_alignment(app, measured_text, scratch, row)
        duration = None
        wait_until = None
        while not returned.is_file():
            retry_at = rejected_receipt.get("retry_at", 0) if rejected_receipt is not None else 0
            if type(retry_at) not in (int, float) or not isfinite(retry_at):
                raise ValueError("The saved provider retry time is invalid.")
            if wait_until is None and retry_at:
                remaining = max(0, retry_at - time())
                original_delay = rejected_receipt.get("retry_delay_seconds", rejected_receipt.get("backoff_seconds"))
                if type(original_delay) in (int, float) and isfinite(original_delay) and original_delay >= 0:
                    remaining = min(remaining, original_delay)
                wait_until = monotonic() + remaining
            if wait_until is not None and (remaining := wait_until - monotonic()) > 0:
                if not resume or on_rate_limit_wait is None:
                    raise ProviderRejected(rejected_receipt["http_status"], retry_at)
                on_rate_limit_wait(time() + remaining)
                while (remaining := wait_until - monotonic()) > 0:
                    if should_stop and should_stop():
                        return None
                    sleep(min(.25, remaining))
            wait_until = None
            if before_submit is not None:
                before_submit()
            with ExitStack() as scope:
                if not scope.enter_context(speech_request_slot(should_stop, on_pacing_wait)):
                    return None
                delay = .25
                while True:
                    if should_stop and should_stop():
                        return None
                    lease = profile.submission(ident, row['source_id']) if hasattr(profile, 'submission') else nullcontext()
                    try:
                        scope.enter_context(lease)
                    except SharedFishBusy:
                        # Only entry failure proves that no POST could happen.
                        # Errors from the submitted body or context cleanup are
                        # outside this loop and remain receipt-governed holds.
                        if should_stop is None:
                            raise
                        if on_wait:on_wait()
                        until = perf_counter() + delay
                        while perf_counter() < until:
                            if should_stop():return None
                            sleep(min(.1, max(0, until-perf_counter())))
                        delay = min(2, delay*1.6)
                        continue
                    break
                if should_stop and should_stop():return None
                if rejected_receipt is not None:
                    save(history / f"rejected-{time_ns()}.json", rejected_receipt)
                if previous_attempt is not None:
                    preserve_attempt(history, previous_attempt)
                    (history / "hold.json").unlink(missing_ok=True)
                    previous_attempt = None
                # Persist the request before POST. An uncertain response cannot be retried automatically.
                save(receipt_path, dict(state="submitting", request_sha256=ident, provider_id=profile.ident,
                                        source_id=row["source_id"], reference=bound, transcript=transcript))
                if keep_studio_exchange:
                    cache_bytes(history / 'request-body.json', wire['body'])
                    save(history / 'request-wire.json', dict(url=wire['url'], body_sha256=digest(wire['body']),
                         request_sha256=ident, provider_id=profile.ident, reference=bound, transcript=transcript))
                on_start(row)
                opener = build_opener(_NoCheckRedirects)
                started = perf_counter()
                try:
                    raw = profile.obtain(wire, opener, on_send=start_speech_request)
                    if keep_studio_exchange:
                        raw_path = history / profile.raw_name
                        cache_bytes(raw_path, raw)
                    audio, provider_timing = check_provider_audio(profile, raw, transcript, name_span)
                except UnusableProviderAudio as error:
                    failure = read(receipt_path)
                    failure.update(state="unusable_return", reason=str(error))
                    if error.__cause__ is not None:
                        failure["validation_detail"] = str(error.__cause__)
                    save(receipt_path, failure)
                    raise InvalidNameTake(str(error), request_id=ident) from error
                except HTTPError as error:
                    scope.callback(error.close)
                    resumable = error.code in RESUMABLE_REJECTIONS
                    previous_backoff = (rejected_receipt or {}).get("backoff_seconds", 0)
                    rejected_receipt = dict(state="provider_rejected" if resumable else "return_requires_inspection",
                                            http_status=error.code, request_sha256=ident)
                    retry_at = provider_retry_at(error)
                    if error.code == 429 and retry_at is None and resume and on_rate_limit_wait is not None:
                        # Some providers omit Retry-After. Back off across repeated explicit
                        # rejections, including after a restart, without submitting other rows.
                        if type(previous_backoff) is not int or not 0 <= previous_backoff <= 300:
                            previous_backoff = 0
                        backoff = min(300, max(30, previous_backoff * 2))
                        rejected_receipt["backoff_seconds"] = backoff
                        retry_at = time() + backoff
                    if retry_at is not None:
                        rejected_receipt["retry_at"] = retry_at
                        rejected_receipt["retry_delay_seconds"] = max(0, retry_at - time())
                        wait_until = monotonic() + rejected_receipt["retry_delay_seconds"]
                    save(receipt_path, rejected_receipt)
                    if retry_at is not None and resume and on_rate_limit_wait is not None:
                        # This response explicitly rejected the request. Leave the submission
                        # slot and wait on its durable retry deadline before the next POST.
                        continue
                    if resumable:
                        raise ProviderRejected(error.code, retry_at) from None
                    raise ValueError(f"The provider returned HTTP {error.code}. Review this request before resubmission.") from None
                except (OSError, ValueError, wave.Error, SubprocessError) as error:
                    # The send may have reached the provider. Only a separately
                    # confirmed retry can replace this attempt, even for a free account.
                    failure = read(receipt_path)
                    failure.update(state="return_requires_inspection", reason=str(error))
                    save(receipt_path, failure)
                    raise ReturnNeedsReview(str(error)) from error
                duration = perf_counter() - started
                # A completed provider call can be reused after alignment or encoding
                # fails. Retention controls what remains once the verified Ogg exists.
                receipt = dict(request_sha256=ident, provider_id=profile.ident,
                    source_id=row["source_id"], reference=bound, transcript=transcript,
                    wire_sha256=digest(wire["body"]), raw_sha256=digest(raw),
                    elapsed_seconds=round(duration,3), retained=keep, verified_return_version=1)
                if transfer := getattr(profile, "last_transfer", None):
                    receipt["transport"] = transfer
                if provider_timing is not None:
                    receipt["word_timing_evidence"] = provider_timing
                record_return(history, audio, receipt)
                cache_bytes(returned, audio)
        delay = .25
        while True:
            if should_stop and should_stop():return None
            on_alignment()
            try:
                evidence = make_alignment(returned, measured_text, configuration)
                save(history / 'phones.json', evidence)
            except PhonemeAlignmentBusy:
                # A completed response is already receipt-bound and retained.
                # Retry this local CPU step only; never return to provider POST.
                if should_stop is None:raise
                if on_alignment_wait:on_alignment_wait()
                until=perf_counter()+delay
                while perf_counter()<until:
                    if should_stop():return None
                    sleep(min(.1,max(0,until-perf_counter())))
                delay=min(2,delay*1.6)
                continue
            break
    return returned, evidence, ident, duration, provider_timing


class CharacterCoverage:
    """Count each eligible catalogue dialogue once, including previously placed names."""

    def __init__(self, rows, area_for):
        self.scopes = {}
        npc_names = {}
        for row in rows:
            area, npc = area_for(row), row["npc_id"]
            self.scopes[source_key(row)] = (("npc", npc), ("zone", area), ("zone_npc", area, npc))
            npc_names[npc] = row["npc"]
        self.totals = Counter(scope for scopes in self.scopes.values() for scope in scopes)
        self.zone_members = {}
        for scope in self.totals:
            if scope[0] == "zone_npc":
                _, area, npc = scope
                self.zone_members.setdefault(area, []).append((npc, npc_names[npc]))
        for area, members in self.zone_members.items():
            first = OPENING_NPCS.get(area)
            members.sort(key=lambda member: (member[0] != first, member[1].casefold(), member[0]))
        self.zone_lists = {}
        self.ready_counts = Counter()
        self.ready_keys = set()

    def set_ready(self, row, ready):
        key = source_key(row)
        if (key in self.ready_keys) == ready:
            return
        scopes = self.scopes[key]
        if ready:
            self.ready_keys.add(key)
            self.ready_counts.update(scopes)
        else:
            self.ready_keys.remove(key)
            self.ready_counts.subtract(scopes)
        _, zone, _ = scopes
        self.zone_lists.pop(zone, None)

    def count(self, rows):
        return {"ready": sum(source_key(row) in self.ready_keys for row in rows), "total": len(rows)}

    def snapshot(self, row):
        result = {"overall": {"ready": len(self.ready_keys), "total": len(self.scopes)}}
        if row is not None:
            npc, zone, _ = self.scopes[source_key(row)]
            area = zone[1]
            label = "Location unknown" if area == "rest" else " ".join(
                word if word == "of" else word.capitalize() for word in area.split("-"))
            for scope, name in ((npc, row["npc"]), (zone, label)):
                result[scope[0]] = {"id": scope[1], "label": name, "ready": self.ready_counts[scope],
                                    "total": self.totals[scope]}
            if zone not in self.zone_lists:
                self.zone_lists[zone] = [
                    {"id": ident, "label": name, "ready": self.ready_counts["zone_npc", area, ident],
                     "total": self.totals["zone_npc", area, ident]}
                    for ident, name in self.zone_members[area]
                ]
            result["zone"]["npcs"] = self.zone_lists[zone]
            first = OPENING_NPCS.get(area)
            if first is not None and self.totals["zone_npc", area, first]:
                result["zone"]["opening_npc_id"] = first
        return result


def run(app, request, status, should_stop=None, on_progress=None, on_activity=None):
    started = perf_counter()
    settings = preferences(request, app.store.setting(PREFS, DEFAULTS))
    if not settings['provider_id']:
        raise ValueError('Choose a configured speech provider before checking coverage or fetching names.')
    character = next((row for row in app.store.setting("characters", [])
                      if row["id"] == settings["character_id"]), None)
    if character is None:
        raise ValueError("Import the WoW settings and select a saved character.")
    values = character["values"]
    if "reported_player_level" in request:
        level = request["reported_player_level"]
        if request.get("automatic") is not True or type(level) is not int or level < 1:
            raise ValueError("The saved automatic fetch has an invalid character level.")
        values = {**values, "level": level}
    name = validate_name(values.get("first_name") or values["name"].split(maxsplit=1)[0])
    spoken = validate_name(values.get("spoken_name") or name)
    from lite_races import profile_race
    race = profile_race(values)
    submit, cached_only, resume, allow_metered = (
        request.get(key, False) for key in ("submit", "cached_only", "resume_provider", "allow_metered"))
    if any(type(flag) is not bool for flag in (submit, cached_only, resume, allow_metered)):
        raise ValueError("Choose valid fetch settings.")
    retry_only = request.get("retry_held_returns", False)
    retry_missing = request.get("fetch_held_with_missing", False)
    resume_interrupted_fetch = request.get("_resume_interrupted_fetch") is True
    if (type(retry_only) is not bool or type(retry_missing) is not bool
            or (retry_only or retry_missing) and (not submit or cached_only or request.get("automatic"))):
        raise ValueError("Retry held recordings requires an explicit online fetch.")
    retry_held = retry_only or retry_missing
    replacement = None
    if "replace_recording" in request or "confirmed_replace" in request:
        from lite_recording_review import selection
        if (request.get("confirmed_replace") is not True or retry_held or not submit or cached_only
                or request.get("automatic") or not getattr(app, "store_managed", False)):
            raise ValueError("Replacing a saved recording requires an explicit desktop fetch.")
        replacement = selection(request.get("replace_recording"))
    replace_occurrence = None
    engine = app.connections.profile(settings["provider_id"]).get("engine")
    addons = addons_folder(request.get("addons_folder") or settings.get("addons_folder"))
    plan, plan_members = imported(app, "plans")
    held_names = getattr(plan_members, "name_fetch_holds", {})
    skyborne_eligibility = getattr(plan_members, "skyborne_eligibility", {})
    stock_fish = engine == "fish" or (engine == "fish_omni" and
                 (getattr(app, "store_managed", False) or bool(getattr(plan_members, "omitted_voice_media", ()))))
    archive, returned_members = imported(app, "returns", required=False) if submit else ({}, {})
    config = app.store.setting(SETTING, {})["plans"]
    store_playback = None
    npc_factions = None
    playback_snapshot = None
    quick_coverage = False
    area_overrides = {}
    plans = installed_plans(addons, plan, gender=str(values.get("gender", "")).casefold())
    if getattr(app, "store_managed", False):
        from lite_store_playback import installed_playback, playable_saved_names
        quick_coverage = not submit and request.get("quick_coverage") is True
        store_playback = installed_playback(addons, check_folders=not quick_coverage)
        if quick_coverage:
            store_playback["coverage_addons"] = addons
        npc_factions = load_npc_factions(addons, config["sha256"])
        area_overrides = load_name_areas(addons, config["sha256"], plans, race=race)
        if not submit:
            playback_snapshot = playable_saved_names(addons)
    placed_store_names = set()
    def area_for(row):
        return starting_greeting_area(row, race) or (
            area_overrides.get(source_key(row), row["area"]) if area_overrides else row["area"])

    def eligible_for_character(row):
        if matches_character(row, race, values, npc_factions, skyborne_eligibility=skyborne_eligibility):
            return True
        if (store_playback is None or not row["source_id"].startswith("q:")
                or not matches_character(row, race, values, skyborne_eligibility=skyborne_eligibility)):
            return False
        # Object and item IDs share a number space with creatures. The exact
        # installed playback must confirm the quest giver's type and wording.
        from lite_paced_name import prepared_base
        try:
            return prepared_base(app, row, store_playback, verify_audio=False,
                                 giver_types=("GameObject", "Item")) is not None
        except (OSError, ValueError, KeyError, TypeError):
            return False

    areas = queue_areas(race, "all" if replacement else settings["coverage"], (area_for(row) for row in plans))
    eligible_catalog = []
    by_area = {area: [] for area in areas}
    for row in plans:
        area = area_for(row)
        if (area in by_area or store_playback is not None) and eligible_for_character(row):
            eligible_catalog.append(row)
            if area not in by_area:
                continue
            if starting_greeting_area(row, race):
                by_area[area].insert(0, row)
            else:
                by_area[area].append(row)
    rows = [row for area, dialogues in by_area.items() for row in ordered_area_dialogues(area, dialogues)]
    area_rows = rows
    selected_keys = request.get('source_keys')
    if selected_keys is not None:
        if (not isinstance(selected_keys, list) or not selected_keys
                or any(not isinstance(key, str) or not key for key in selected_keys)
                or len(set(selected_keys)) != len(selected_keys)):
            raise ValueError('Select a nonempty list of unique prepared dialogue source keys.')
        selection = set(selected_keys)
        eligible = {source_key(row) for row in rows}
        if not selection <= eligible:
            raise ValueError('A selected dialogue is unavailable for this character and coverage.')
        rows = [row for row in rows if source_key(row) in selection]
    if replacement:
        from lite_paced_name import prepared_base
        from lite_recording_review import current_take
        from lite_store_playback import slot_key
        current_take(addons, name.casefold(), replacement)
        revision = replacement["slot"].split(":", 1)[0]
        matches = []
        for row in rows:
            candidates = store_playback["seals_by_dialogue"].get((row["source_id"], row["npc_id"]), ())
            if not any(seal["clock_contract_sha256"] == revision for _, seal in candidates):
                continue
            base = prepared_base(app, row, store_playback, verify_audio=False)
            if base:
                for index, part in enumerate(base[1]["sequence"], 1):
                    if part["kind"] == "name" and slot_key(base[2], index) == replacement["slot"]:
                        matches.append((row, part["occurrence"]))
        if len(matches) != 1:
            raise ValueError("This voice pack no longer supplies the original recording. You can still listen to the saved take.")
        selected_row, replace_occurrence = matches[0]
        rows = [selected_row]
    previous_returns = PreviousNameReturns(app.folder / "spoken-player-names/requests")

    def legacy_conflict(row, base, snapshot=None):
        from lite_store_playback import review_saved_returns
        _, manifest, seal, _ = base
        return review_saved_returns(addons, row, manifest, seal, name, spoken, settings["provider_id"],
                                    previous_returns, confirmed=retry_held, playback_snapshot=snapshot)

    def load_ready_dialogues(candidate_rows, snapshot):
        from lite_paced_name import prepared_base
        from lite_store_playback import name_present

        value = name.casefold()
        saved_revisions = {slot.split(":", 1)[0] for slot, choices in snapshot.items()
                           if value in choices}
        retained = store_playback.get("retained", {})
        for row in candidate_rows:
            candidates = store_playback.get("seals_by_dialogue", {}).get((row["source_id"], row["npc_id"]), ())
            revisions = {seal["clock_contract_sha256"] for _, seal in candidates}
            previous = {seal["clock_contract_sha256"] for revision in revisions
                        for _, seal in retained.get(revision, ())}
            if not (revisions | previous) & saved_revisions:
                continue
            try:
                current = prepared_base(app, row, store_playback, verify_audio=False)
                if current and not legacy_conflict(row, current, snapshot) and name_present(addons, current[1], current[2], value, store_playback,
                                            playback_snapshot=snapshot):
                    coverage.set_ready(row, True)
            except (OSError, ValueError, KeyError, TypeError):
                # A missing clip or mismatched receipt does not count as ready.
                continue
    coverage = None
    progress_row = next(iter(rows), None)
    if store_playback is not None:
        coverage = CharacterCoverage(eligible_catalog, area_for)
        load_ready_dialogues(eligible_catalog, playable_saved_names(addons) if submit else playback_snapshot)
        if quick_coverage:
            return {"coverage_count": coverage.count(rows), "coverage_progress": coverage.snapshot(progress_row)}
    work = app.folder / "spoken-player-names"
    packages = work / "packages" / config["sha256"]
    queue = work / "requests"
    work.mkdir(parents=True, exist_ok=True)
    seed_cache = work / "prepared-companions"
    if not request.get("_keep_fetch_preferences"):
        app.store.set_setting(PREFS, settings)
    timing = PhaseTiming()
    staged_paths = set()
    statuses, placement_errors = [], []
    delivery = {}
    paced_pending = None
    coverage_paced_state = None
    paced_index_expected = None
    pending_discard = {}
    disabled_stock_paths = set()
    placed = 0
    waiting_placement = None
    dialogue_number = 0
    npc_seconds = {}
    retake_attempt = 0
    npc_numbers = {npc: index for index, npc in enumerate(dict.fromkeys(row["npc_id"] for row in rows), 1)}
    request_id = request.get("game_request_id")
    reload_checkpoint = {}
    newly_ready = 0
    current_area = None
    listener_addons = request.get("_game_listener_addons")
    background = bool(request_id or (listener_addons and addons == Path(listener_addons)))
    live_hud = getattr(app, "live_hud", lambda: False)

    def update(phase, row=None, eta=None, detail=None, *, instance_progress=None, show_excerpt=True):
        active = row or {}
        count = len(active.get("name_slots", ())) or 1
        occurrence = active.get("name_occurrence", 1)
        name_progress = dict(name_occurrence=occurrence, name_total=count, dialogue_number=dialogue_number,
                             npc_number=npc_numbers[active["npc_id"]], npc_total=len(npc_numbers),
                             instance_progress=instance_progress) if active else {}
        words = active.get("stock_text", "").split(maxsplit=8) if show_excerpt else []
        excerpt = " ".join(words[:8])[:75]
        if len(words) > 8 or len(excerpt) == 75:
            excerpt += "â€¦"
        if on_activity and row:
            on_activity(dict(phase=phase, npc=row["npc"], source_id=row["source_id"],
                             npc_started_at=dialogue_started - npc_seconds.get(row["npc_id"], 0),
                             npc_sampled_at=perf_counter() if instance_progress == 100 else None,
                             stage=dialogue_stage(row["source_id"]), ready=placed,total=len(rows),
                             eta_seconds=eta, started_at=time(), detail=detail, excerpt=excerpt,
                             **({"coverage_progress": coverage.snapshot(row)} if coverage is not None else {}),
                             **({"retake_attempt": retake_attempt, "retake_total": MAX_NAME_RETAKES}
                                if retake_attempt else {}),
                             **name_progress, **reload_checkpoint))
        elif on_activity and coverage is not None and phase == "planning":
            on_activity(dict(phase=phase, ready=placed, total=len(rows),
                             coverage_progress=coverage.snapshot(progress_row)))
        if submit:
            if active and count > 1:
                label = f"Name {occurrence} of {count}."
                detail = f"{label} {detail}" if detail else label
            try:
                publish_snapshot(addons, state=phase, ready=placed,total=len(rows), npc=active.get("npc"),
                                 source_id=active.get("source_id"),eta_seconds=eta,
                                 request_id=request_id,background=background,detail=detail,character=values,
                                 provider=settings["provider_id"] if background else None,
                                 coverage=settings["coverage"] if background else None,
                                 settings_port=request.get("_game_settings_port"),
                                 live_hud=live_hud() if background else False, excerpt=excerpt,
                                 **name_progress)
            except (OSError, ValueError) as error:
                logging.getLogger(__name__).warning("Could not write the Lite progress snapshot (%s)", error)

    def place():
        nonlocal placed,paced_pending
        if store_playback is not None:
            placed = len(staged_paths & placed_store_names)
            delivery["packaged"] = placed
            for key in pending_discard.keys() & placed_store_names:
                for receipt in pending_discard.pop(key):
                    discard_pending_return(queue, receipt)
            return
        first_install = not (addons / ADDON / (ADDON + ".toc")).is_file()
        current = {source_key(row): row for row in plans}
        mismatched = {}
        from lite_paced_delivery import SETTING as PACED_SETTING,canonical,validate_projection
        from lite_paced_name import managed_name_matches
        paced=paced_pending or app.store.setting(PACED_SETTING,{}) or {}
        projections = {}
        covered = {}
        projection_keys = {}
        for path,seal in paced.get('seals',{}).items():
            directory=filesystem_path(path);projected=read(directory/'dialogue.json')
            validate_projection(directory,projected,seal)
            projection_key = manifest_key(projected)
            projection_keys[path] = projection_key
            metadata=read(directory/'native-selection-metadata.json')
            extension=metadata.get('personalized_source_extension',{})
            if extension.get('actual_name_manifest_canonical_sha256') and extension.get('source_contract_sha256'):
                generation_key = extension.get('generation_source_key', 'native:' + extension['source_contract_sha256'])
                projections[generation_key] = (extension, projection_key, projected)
            if projected.get('expanded_name_extension') and (binding := projected.get('original_spoken')):
                row = current.get(binding['path'])
                if (row and signed_binding(row) == binding and row['source_id'] == projected['source_id']
                        and row['npc_id'] == projected['npc_id']
                        and any(name.casefold() in part['choices'] for part in projected['sequence'] if part['kind'] == 'name')):
                    covered[binding['path']] = projection_key

        def compatible(manifest):
            if manifest.get('paced_source'):
                # The package writer rechecks the externally pinned contract,
                # metadata and actual Ogg clocks for every admitted typed route.
                return True
            key = manifest_key(manifest)
            projection = projections.get(key)
            row = current.get(key)
            if projection:
                extension, projection_key, projected = projection
                canonical_native = digest(canonical(manifest))
                same_recipe = extension["actual_name_manifest_canonical_sha256"] == canonical_native
                same_source = False
                if row:
                    identity = native_identity(row)
                    source_hash = identity["source_contract_sha256"] if identity else digest(canonical(row))
                    seed = row.get("companion_seed", {}).get("manifest_sha256")
                    same_source = (source_hash == extension["source_contract_sha256"] and seed
                                   and seed == extension.get("root_native_manifest_sha256")
                                   and manifest.get("companion_seed") == row.get("companion_seed"))
                if same_recipe or same_source:
                    if managed_name_matches(manifest, projected, extension, name.casefold(), canonical_native):
                        covered[key] = projection_key
                    # A staged name that still needs pacing cannot displace
                    # the already admitted route for the same dialogue.
                    return False
            if manifest.get("quest_variant") or (row and row.get("requires_paced_base")):
                # A native intermediate must never compete with the sealed
                # fallback when its final-paced Name join is held.
                return False
            if row:
                try:
                    installed(addons, row)
                except ValueError:
                    mismatched[row["stock_path"]] = row["stock_sha256"]
                    return False
                except OSError:
                    return False
                try:
                    if not matches_prepared_dialogue(manifest, row):
                        return False
                    if manifest.get("reconstructed_caption") != reconstructed_caption(row):
                        return False
                    installed_companion(addons, row, members=plan_members, cache=seed_cache)
                    return True
                except (OSError, ValueError):
                    pass
            return False

        with hold(work,'.paced-import.lock'),TemporaryDirectory(dir=work,prefix='.placement-') as temporary:
            if paced_pending is not None and '_prepared_from_collection_sha256' in paced_pending:
                live=app.store.setting(PACED_SETTING,{}) or {}
                if live.get('collection_sha256')!=paced_pending['_prepared_from_collection_sha256']:
                    raise ValueError('The paced collection changed during Name preparation. Retry to preserve its new routes.')
            elif paced:
                live=app.store.setting(PACED_SETTING,{}) or {}
                if live.get('collection_sha256')!=paced.get('collection_sha256'):
                    raise ValueError('The paced collection changed before placement. Retry to preserve its new routes.')
            from lite_portable_package import INDEX
            active_index=addons/INDEX
            expected_index=(paced_index_expected if paced_pending is not None
                            else file_hash(active_index) if active_index.is_file() else None)
            if (file_hash(active_index) if active_index.is_file() else None)!=expected_index:
                raise ValueError('The installed companion changed during Name preparation. Retry to preserve its new routes.')
            archive=Path(temporary)/ARCHIVE
            package = package_names(packages, archive, include=compatible,
                                    trusted_deliveries=paced.get('seals'), legacy_addons=addons,
                                    preserved_paced_names=paced.get('preserved_name_seals'))
            included = set(package.pop('packaged_source_keys', package.get('packaged_stock_paths', [])))
            package.pop('packaged_stock_paths',None)
            if not set(projection_keys.values()) <= included:
                raise ValueError('The cumulative package omitted an admitted paced route.')
            delivery.update(package)
            if delivery["packaged"]:
                # The replacement index already excludes these stale routes.
                # Activate it against the original index before any fallback edit.
                try:
                    install_names(archive, addons,check_index=True,expected_index=expected_index)
                except (OSError, ValueError):
                    try:
                        disable_routes(mismatched)
                    except (OSError, ValueError) as error:
                        placement_errors.append(dict(dialogue="Spoken playback index", reason=str(error)))
                    raise
                with hold(work, ".package.lock"):
                    archive.replace(work / ARCHIVE)
                delivery["restart_required"] = delivery.get("restart_required", False) or first_install
                delivery["download"] = "/api/export/spoken-player-names"
                if paced:
                    placed_state=dict(paced)
                    placed_state['placement_index_sha256']=file_hash(addons/INDEX)
                    placed_state['installed_index_sha256']=(placed_state['placement_index_sha256']
                        if all(key.startswith('paced:') for key in included) else None)
                    placed_state.pop('_prepared_from_collection_sha256',None)
                    app.store.set_setting(PACED_SETTING,placed_state)
                    paced_pending=None
                installed_keys = included | {key for key, projection in covered.items() if projection in included}
                placed = len(staged_paths.intersection(installed_keys))
                for key in pending_discard.keys() & installed_keys:
                    for receipt in pending_discard[key]:
                        discard_pending_return(queue, receipt)
                    del pending_discard[key]
                if not settings['keep_raw_wav']:
                    from lite_paced_name import discard_derived_audio
                    from lite_expanded_name_stage import discard_placed_units
                    installed_seals = {path: seal for path, seal in paced.get('seals', {}).items()
                                       if projection_keys[path] in included}
                    installed_state = {'seals': installed_seals}
                    discard_derived_audio(app, installed_state)
                    discard_placed_units(app, installed_state)
            else:
                disable_routes(mismatched)

    def flush_staged():
        nonlocal waiting_placement
        if waiting_placement is None:
            return
        dialogue, waiting_placement = waiting_placement, None
        try:
            place()
        except (ValueError, OSError) as error:
            placement_errors.append(dict(dialogue=dialogue, reason=str(error)))

    def capture_paced_placement():
        nonlocal paced_index_expected
        if paced_pending is None:
            from lite_portable_package import INDEX
            from lite_paced_delivery import SETTING as PACED_SETTING
            active_index=addons/INDEX
            paced_state=app.store.setting(PACED_SETTING,{}) or {}
            paced_index_expected=paced_state.get('placement_index_sha256',paced_state.get('installed_index_sha256'))
            if (file_hash(active_index) if active_index.is_file() else None)!=paced_index_expected:
                raise ValueError('The installed companion changed since verified placement. Refresh coverage to reconcile its routes before fetching this Name.')

    def pace_native(row, folder, stock=None, *, archived_returns=None, seed_joins=None, request_row=None):
        nonlocal paced_pending
        from lite_paced_name import stage_name,prepared_base
        state = store_playback if store_playback is not None else paced_pending
        if prepared_base(app,row,state) is None:return False
        if store_playback is None:
            capture_paced_placement()

        def publish_store_name(seal, current, clock, caption, value, payloads):
            from lite_store_playback import publish_names, request_identity
            request = request_identity(row, spoken, settings["provider_id"])
            publish_names(addons, store_playback, seal, current, clock, caption, value, payloads,
                          request=request, replacement=replacement)
            placed_store_names.add(source_key(row))
            # A single-slot replacement can leave another occurrence unavailable.
            if replacement:
                from lite_store_playback import name_present
                coverage.set_ready(row, name_present(addons, current, seal, value, store_playback))
            else:
                coverage.set_ready(row, True)

        def alignment(transcript,scratch,slot):
            configuration=prepare_alignment(app,transcript,scratch,slot)
            return lambda audio,identity,text:align(audio,identity,text,configuration)
        def check_placeholder(clip, words, onset, slot):
            from lite_placeholder_audio import placeholder_evidence
            config = alignment_profile(app.connections.parser())
            return placeholder_evidence(clip, words, onset, config, pronunciations=slot.get('pronunciations'))

        status('Preparing complete contextual recordings at the selected pace.')
        def verify_clip(slot):
            update("aligning", slot, detail="Verifying the Ogg and its playback joins.", instance_progress=75)

        delay=.25
        while True:
            if should_stop and should_stop():return False
            try:
                pending=stage_name(app,request_row or row,folder,name,spoken,settings['provider_id'],plan_members,queue,status,
                                   alignment,state=state,stock=stock,archived_returns=archived_returns,
                                   publish_store_name=publish_store_name if store_playback is not None else None,
                                   store_addons=addons if store_playback is not None else None,
                                   only_occurrence=replace_occurrence, on_verify_clip=verify_clip, seed_joins=seed_joins,
                                   check_placeholder=check_placeholder if stock_fish else None)
            except PhonemeAlignmentBusy:
                if should_stop is None:raise
                waiting_for_alignment(row)
                until=perf_counter()+delay
                while perf_counter()<until:
                    if should_stop():return False
                    sleep(min(.1,max(0,until-perf_counter())))
                delay=min(2,delay*1.6)
                continue
            break
        if pending is None:return False
        if store_playback is None:
            paced_pending=pending
        return True

    def disable_routes(stock_paths, *, expected_render=None):
        if store_playback is not None or not submit or not stock_paths:
            return
        affected = disable_installed_routes(addons, stock_paths, expected_render=expected_render)
        disabled_stock_paths.update(affected)
        if affected:
            disable_exported_routes(work / ARCHIVE, affected)

    def checked_stock(row):
        try:
            return installed(addons, row)
        except ValueError as failure:
            if (isinstance(failure, MissingStockRecording) and stock_fish and store_playback is not None
                    and row.get("analysis_base") and row.get("reference_contract")
                    and not row.get("expanded_name_binding")):
                return None
            if row.get('native_source'):raise
            try:
                disable_routes({row["stock_path"]: row["stock_sha256"]})
            except (OSError, ValueError) as error:
                placement_errors.append(dict(dialogue=row["key"], reason=str(error)))
            raise

    def fetch_expanded(row, binding):
        nonlocal paced_pending
        from lite_expanded_binding import local_base
        from lite_expanded_name_stage import stage, require_retained_choice
        from lite_expanded_return_cut import returned_request
        bound, choices = binding
        stock = checked_stock(row)
        if not submit:
            if stock_fish:
                require_hosted_playback_base(row)
            return {"state":"queued"}
        flush_staged()
        transcript, _ = name_text(row, spoken)
        capture_paced_placement()
        if name.casefold() in choices:
            request = require_retained_choice(app,bound,name,transcript_digest(transcript),
                                              settings["provider_id"],choices[name.casefold()])
            pending_discard[source_key(row)] = {request}
            return {"state":"staged", "dialogue":row["key"]}
        if paced_pending is not None:
            raise ValueError("Finish the earlier pending placement before extending this expanded collection.")
        if stock_fish:
            require_hosted_playback_base(row)
        with TemporaryDirectory(dir=work,prefix=".expanded-name-") as temporary:
            scratch=Path(temporary)
            base, bound = local_base(scratch,row,plan_members,stock,bound)
            profile=NameProvider(app.connections, settings["provider_id"], submit=not cached_only,
                                 allow_metered=allow_metered, timestamps=True,
                                 lossless_references=store_playback is not None)
            hosted_refs = hosted_stock_reference(row,stock,scratch,plan_members) if stock_fish else None
            refs = None if stock_fish else verified_references(row,plan_members)[0]
            request_references = reference_evidence(hosted_refs) if stock_fish else None
            def verify_cached(raw, receipt, ident):
                returned_request(raw,receipt,bound,row,spoken,ident,profile.ident,
                                 request_references=request_references)
            take=obtain_fresh(app,row,transcript,profile,stock,queue,scratch,settings["keep_raw_wav"],resume,
                              lambda active:update("fetching",active),lambda:update("aligning",row),
                              cached_only=cached_only,prepared_references=refs,should_stop=should_stop,
                              hosted_references=hosted_refs,
                              on_wait=lambda:waiting_for_shared(row),on_alignment_wait=lambda:waiting_for_alignment(row),
                              on_pacing_wait=lambda:waiting_for_pacing(row),
                              before_submit=check_provider_cooldown,
                              on_rate_limit_wait=lambda retry_at: waiting_for_rate_limit(row, retry_at),
                              verify_cached=verify_cached, name_span=name_text(row, spoken)[1],
                              retry_held=retry_held, retry_missing=retry_missing, on_receipt=row_attempts.append,
                              resume_interrupted_fetch=resume_interrupted_fetch)
            if take is None or (should_stop and should_stop()):
                return {"state":"queued"}
            raw, phones, ident, _, _ = take
            receipt=read(queue/ident/"receipt.json")
            configuration=prepare_alignment(app,transcript,scratch,row)

            def align_expanded(audio, identity, text):
                active_configuration = configuration if text == transcript else prepare_alignment(app, text, scratch, row)
                return align(audio, identity, text, active_configuration)

            paced_pending=stage(app,row,bound,name,spoken,raw,receipt,phones,ident,profile.ident,base,
                                align_expanded,status,request_references=request_references)
            pending_discard[source_key(row)] = {ident}
        return {"state":"staged", "dialogue":row["key"]}

    def cached_store_name(row, paced_base):
        from lite_store_playback import name_present

        _, manifest, seal, _ = paced_base
        if replacement:
            return None
        if not name_present(addons, manifest, seal, name.casefold(), store_playback,
                            playback_snapshot=playback_snapshot):
            coverage.set_ready(row, False)
            return None
        if legacy_conflict(row, paced_base):
            coverage.set_ready(row, False)
            if retry_held:
                return None
            raise ReturnNeedsReview("Saved provider words contradict this name. Review and retry the recording.")
        if submit:
            placed_store_names.add(source_key(row))
        coverage.set_ready(row, True)
        return {"state": "staged", "dialogue": row["key"], "cached_placement": True}

    def process(row, *, request_row=None, package_root=None, retake_request=None):
        from lite_expanded_name_stage import checked_binding
        from lite_paced_name import preflight_name_base

        if reason := held_names.get(source_key(row)):
            # A catalog can retain coverage information after removing the old
            # reference. A previously installed name can still be playable.
            if store_playback is not None and (submit or playback_snapshot):
                try:
                    base = preflight_name_base(app, row, store_playback, verify_audio=False)
                    if base and (cached := cached_store_name(row, base)):
                        return cached
                except (OSError, ValueError):
                    pass
            if stock_fish and row.get("native_source") and reason == LEGACY_MISSING_SPOKEN:
                raise OriginalSpokenNotCatalogued(row)
            return {"state": "held", "reason": reason}

        if binding:=checked_binding(app,row,plan_members):
            if store_playback is not None:
                raise ValueError("This dialogue is awaiting its CurseForge Name placement support.")
            return fetch_expanded(row,binding)
        state = store_playback if store_playback is not None else paced_pending if submit else coverage_paced_state
        paced_base = preflight_name_base(app, row, state, verify_audio=submit)
        if store_playback is not None and not paced_base:
            raise ValueError("Update DMSpeaks for WoW in CurseForge to get this dialogue's playback files.")
        stock = checked_stock(row)
        if store_playback is not None and (submit or playback_snapshot):
            if cached := cached_store_name(row, paced_base):
                return cached

        if stock_fish:
            require_hosted_playback_base(row, stock_missing=stock is None, members=plan_members, playback=paced_base)
        if row.get('native_reference_hold'):
            raise ValueError(row['native_reference_hold'])
        companion = installed_companion(addons, row, members=plan_members, cache=seed_cache,
                                        verify_only=not submit, store_managed=store_playback is not None)
        seed_joins = companion.joins if companion else None
        sealed_clips = seed_joins.clips if seed_joins else None
        if not (row.get('native_source') or row.get("quest_variant")) and (
                row.get("companion_seed") or row.get("reference_contract")):
            expected_render = render_fingerprint(
                row["pcm_sha256"], row.get("companion_seed"), row.get("analysis_base", {}).get("sha256"),
                reconstructed_caption(row), row.get("reference_contract"))
            try:
                disable_routes({row["stock_path"]: row["stock_sha256"]}, expected_render=expected_render)
            except (OSError, ValueError) as error:
                placement_errors.append(dict(dialogue=row["key"], reason=str(error)))
        folder = dialogue_folder(package_root if package_root is not None else packages, row)
        if not submit:
            return {"state":"queued"}
        pending = []
        receipts = set()
        archived_returns = {}
        final_delivery_ready=not row.get('native_source') or row['native_delivery_policy']['final_paced_delivery_ready']
        if paced_base:
            final_delivery_ready=False
        if request_row is None:
            request_row = continuing_name_context(row) if stock_fish and store_playback is not None else row
        if (stock_fish and store_playback is not None and selected_keys and retry_missing
                and request.get("retry_reviewed_dialogues") is True):
            request_row = following_context_for_name_retry(request_row)
        for slot in name_occurrences(request_row):
            if replacement and slot.get("name_occurrence", 1) != replace_occurrence:
                continue
            slot = resolve_name_article(app, slot, spoken)
            transcript, _ = name_text(slot, spoken)
            item = None
            if not replacement and (not stock_fish or cached_only):
                item = saved_match(archive.get("returns", []), slot, name, transcript, settings["provider_id"])
            if item is not None:
                archived_returns[item["request_sha256"]] = (item, returned_members[item["wav"]])
            existing = recent_choice(folder, slot, name, transcript, settings["provider_id"],
                                     sealed_clips=sealed_clips)
            if existing:
                receipts.add(existing["request_sha256"])
            else:
                pending.append((slot, transcript, item))
        if not pending:
            if paced_base:
                flush_staged()
                final_delivery_ready = pace_native(row, folder, stock, archived_returns=archived_returns,
                                                   seed_joins=seed_joins, request_row=request_row)
            if final_delivery_ready:
                pending_discard[source_key(row)] = receipts
            return {"state":"staged","dialogue":row["key"],"final_delivery_pending":not final_delivery_ready,
                    "cached_placement":not paced_base}
        flush_staged()
        if cached_only and any(item is None for _, _, item in pending) and not any(queue.glob("*/return.wav")):
            return {"state":"queued"}
        with TemporaryDirectory(dir=work, prefix=".stock-") as temporary:
            scratch = Path(temporary)
            reference = scratch / "stock.wav"
            separate_reference = (row.get("analysis_base") and row.get("requires_paced_base")
                                  and signed_source_needed(row, plan_members))
            signed_reference = scratch / "signed-dialogue.wav" if separate_reference else reference
            base = render_inputs(stock, row, reference, plan_members, signed_reference=signed_reference)
            hosted_refs = (hosted_stock_reference(row, stock, scratch, plan_members, decoded_original=reference)
                           if stock_fish and any(item is None for _, _, item in pending) else None)
            staged = scratch / "dialogue" if "name_slots" in row else folder
            if staged != folder and folder.is_dir():
                copytree(folder, staged)
            for slot, transcript, item in pending:
                if should_stop and should_stop():
                    return {"state":"queued"}
                update("planning", slot, detail="Preparing the request.", instance_progress=0)
                take_dir = scratch
                if "name_occurrence" in slot:
                    take_dir = scratch / str(slot["name_occurrence"])
                    take_dir.mkdir()
                try:
                    receipt = render_occurrence(staged, slot, transcript, item, reference, base, take_dir, companion,
                                                signed_reference=signed_reference, hosted_references=hosted_refs,
                                                retake_request=retake_request)
                except (PhonemeAlignmentBusy, ProviderRejected, NameCutUnavailable):
                    raise
                except ValueError as error:
                    if "name_occurrence" not in slot:
                        raise
                    raise ValueError(f"Name occurrence {slot['name_occurrence']}: {error}") from error
                if receipt is None:
                    return {"state":"queued"}
                if receipt:
                    receipts.add(receipt)
            if staged != folder:
                # A repeated-name dialogue becomes visible only once all of its
                # Name choices have rendered. Completed returns survive retries.
                if folder.is_dir():
                    for clip in staged.glob("*.ogg"):
                        target = folder / clip.name
                        if not target.is_file() or file_hash(target) != file_hash(clip):
                            cache_bytes(target, clip.read_bytes())
                    cache_bytes(folder / "dialogue.json", (staged / "dialogue.json").read_bytes())
                else:
                    folder.parent.mkdir(parents=True, exist_ok=True)
                    staged.rename(folder)
            # Verify the published files as well as the render staging folder.
            # A failed copy must not consume the only recoverable WAV.
            validate_routes(folder, read(folder / "dialogue.json"), folder, sealed_clips=sealed_clips)
            if paced_base:
                final_delivery_ready = pace_native(row, folder, stock, archived_returns=archived_returns,
                                                   seed_joins=seed_joins, request_row=request_row)
            if final_delivery_ready:
                pending_discard[source_key(row)] = receipts
        return {"state":"staged","dialogue":row["key"],"final_delivery_pending":not final_delivery_ready}

    def render_occurrence(folder, row, transcript, item, reference, base, scratch, companion, *,
                          signed_reference, hosted_references, retake_request=None):
        from lite_portable_references import references_for_stock

        alignment_kind = "aligning-saved-phones" if item is not None else "aligning-waveform"
        alignment_started = None
        provider_timing = None

        def begin_alignment():
            nonlocal alignment_started
            alignment_started = perf_counter()
            update("aligning", row, timing.estimate(alignment_kind, len(transcript)),
                   instance_progress=50)

        if item is not None:
            audio, phones, ident = scratch / "return.wav", item["phones"], item["request_sha256"]
            cache_bytes(audio, returned_members[item["wav"]])
            begin_alignment()
        else:
            provider = NameProvider(app.connections, settings["provider_id"], submit=not cached_only,
                                    allow_metered=allow_metered, timestamps=True,
                                    lossless_references=store_playback is not None)
            size = len(transcript) + reference.stat().st_size // 4096
            take = obtain_fresh(app, row, transcript, provider,
                reference, queue, scratch, settings["keep_raw_wav"], resume,
                lambda active: update("fetching", active, timing.estimate("fetching", size), instance_progress=25),
                begin_alignment, cached_only=cached_only,
                prepared_references=references_for_stock(row, plan_members, signed_reference)
                                    if hosted_references is None and "reference_contract" in row else None,
                hosted_references=hosted_references,
                should_stop=should_stop, on_wait=lambda: waiting_for_shared(row),
                on_pacing_wait=lambda: waiting_for_pacing(row),
                before_submit=check_provider_cooldown,
                on_rate_limit_wait=lambda retry_at: waiting_for_rate_limit(row, retry_at),
                on_alignment_wait=lambda: waiting_for_alignment(row), alignment_text=expanded(transcript),
                name_span=name_text(row, spoken)[1], retry_held=retry_held, retry_missing=retry_missing,
                on_receipt=row_attempts.append,
                replace_saved=bool(replacement), resume_interrupted_fetch=resume_interrupted_fetch,
                retake_request=retake_request, retry_uncertain=bool(selected_keys))
            if take is None or (should_stop and should_stop()):
                return None
            audio, phones, ident, elapsed, provider_timing = take
            timing.measure("fetching", size, elapsed)
        checked_stock(row)
        installed_companion(addons, row, members=plan_members, cache=seed_cache,
                            store_managed=store_playback is not None)
        render(folder, row, base, audio, phones, name, spoken, ident,
               provider_id=item["provider_id"] if item else settings["provider_id"],
               companion=companion, provider_timing=provider_timing)
        timing.measure(alignment_kind, len(transcript), perf_counter() - alignment_started)
        return ident if item is None else ""

    def process_with_retake(row):
        nonlocal retake_attempt
        allow_retakes = stock_fish and store_playback is not None and submit and not cached_only and not replacement
        limit = MAX_NAME_RETAKES if allow_retakes else 0
        failed_request = None
        next_row = row
        try:
            for retake_attempt in range(limit + 1):
                try:
                    if not retake_attempt:
                        return process(row)
                    if should_stop and should_stop():
                        return {"state": "queued"}
                    update("planning", row, detail="Preparing another take for this dialogue.")
                    with TemporaryDirectory(dir=work, prefix=".name-retake-") as temporary:
                        return process(row, request_row=next_row, package_root=Path(temporary),
                                       retake_request=failed_request)
                except NameCutUnavailable as error:
                    if retake_attempt == limit or error.request_id is None:
                        error.retake_attempts = retake_attempt
                        raise
                    failed_request = error.request_id
                    if not isinstance(error, InvalidNameTake) and not retry_only:
                        next_row = following_context_for_name_retry(row)
        finally:
            retake_attempt = 0

    def waiting_for_shared(row):
        status('Waiting for the shared Fish service. Pause is available.')
        update('planning', row, detail='Waiting for the shared Fish service. Pause is available.')

    def waiting_for_alignment(row):
        status('Waiting for the shared phoneme aligner. Pause is available.')
        update('aligning', row, detail='Waiting for the shared phoneme aligner. Pause is available.')

    def waiting_for_pacing(row):
        detail = 'Waiting briefly before the next speech request.'
        status(detail)
        update('planning', row, detail=detail)

    def waiting_for_rate_limit(row, retry_at):
        wait_seconds = max(1, round(retry_at - time()))
        detail = f"The speech provider is limiting requests. Fetch will retry automatically in about {wait_seconds} seconds."
        status(detail)
        update("planning", row, detail=detail)

    provider_resume_at = 0

    def check_provider_cooldown():
        if time() < provider_resume_at:
            raise ProviderRejected(429, provider_resume_at)

    update("planning")
    managed_scratch = (TemporaryDirectory(dir=work, prefix=".name-overlay-")
                       if store_playback is not None else nullcontext())
    with hold(work, ".name-processing.lock"), managed_scratch as scratch:
        if scratch is not None:
            if submit:
                from lite_store_playback import finish_placed_returns
                finish_placed_returns(addons, queue)
            packages = Path(scratch) / "dialogues"
            seed_cache = Path(scratch) / "references"
        elif submit:
            carry_compatible(app, packages, plans, members=plan_members)
        else:
            from lite_paced_delivery import SETTING as PACED_SETTING
            coverage_paced_state = app.store.setting(PACED_SETTING, {}) or {}
        row_attempts = []
        for dialogue_number, row in enumerate(rows, 1):
            row_attempts.clear()
            identity = dict(source_id=row["source_id"], source_key=source_key(row),
                            npc_id=row["npc_id"], npc=row["npc"], area=area_for(row))
            if should_stop and should_stop():
                statuses.append(dict(identity, state="queued"))
                continue
            dialogue_started = perf_counter()
            progress_row = row
            status(f"Checking {row['npc']} Â· {dialogue_stage(row['source_id'])}")
            if (submit and current_area != identity["area"]
                    and newly_ready > reload_checkpoint.get("reload_ready", 0)):
                reload_checkpoint = {"reload_ready": newly_ready, "reload_at": time()}
            current_area = identity["area"]
            if submit and source_key(row) not in held_names:
                update("planning", row, detail="Preparing the dialogue.", instance_progress=0)
            previous_placement_errors = len(placement_errors)
            try:
                outcome = process_with_retake(row)
            except OriginalSpokenNotCatalogued as error:
                outcome = {"state": "source_unavailable", "reason": str(error)}
            except ProviderRejected as error:
                if error.retry_at is not None:
                    provider_resume_at = max(provider_resume_at, error.retry_at)
                outcome = dict(state="provider_rejected", http_status=error.http_status, reason=str(error))
            except ReturnNeedsReview as error:
                outcome = dict(state="held", reason=str(error), retryable_return=True)
            except PhonemeAlignmentBusy:
                outcome = dict(state="returned", reason="The shared phoneme aligner is occupied.")
            except (ValueError, OSError, SubprocessError, wave.Error) as error:
                reason = str(error)
                state = "waiting_for_base" if "signed Spoken recording" in reason else "held"
                outcome = dict(state=state, reason=reason)
                failed_request = row_attempts[-1] if row_attempts else None
                if isinstance(error, NameCutUnavailable):
                    failed_request = error.request_id or failed_request
                    if error.retake_attempts == MAX_NAME_RETAKES:
                        outcome["retake_attempts"] = error.retake_attempts
                        outcome["reason"] = reason.rstrip(".") + f". All {MAX_NAME_RETAKES} automatic retries were unsuccessful."
                if failed_request and isinstance(error, ValueError):
                    # Preserve the failed occurrence. Other takes from the same
                    # multi-name dialogue can still be reused on the next run.
                    history = queue / failed_request
                    try:
                        if history.is_dir():
                            with hold(history):
                                path = history / "receipt.json"
                                receipt = read(path) if path.is_file() else {}
                                if receipt.get("state") == "unusable_return":
                                    outcome["retryable_return"] = True
                                if receipt.get("state") == "returned" and receipt.get("wav_sha256"):
                                    marker = dict(wav_sha256=receipt["wav_sha256"], reason=reason)
                                    if isinstance(error, NameCutUnavailable):
                                        marker["cut_review_version"] = 2
                                    save(history / "hold.json", marker)
                                    outcome["retryable_return"] = True
                    except (OSError, ValueError):
                        logging.getLogger(__name__).warning("Could not save the held return for %s.",
                                                            row["source_id"], exc_info=True)
            except Exception as error:
                logging.getLogger(__name__).exception("Could not process dialogue %s.", row["source_id"])
                outcome = dict(state="held", reason=f"Dialogue processing failed ({type(error).__name__}).")
            cached = outcome.pop("cached_placement", False)
            statuses.append(dict(identity, **outcome))
            if submit and outcome["state"] == "staged":
                staged_paths.add(source_key(row))
                waiting_placement = row["key"]
                if not cached or not placed:
                    flush_staged()
                if source_key(row) in placed_store_names and not cached:
                    newly_ready += 1
                    if newly_ready % 20 == 0:
                        reload_checkpoint = {"reload_ready": newly_ready, "reload_at": time()}
                    for slot in name_occurrences(row):
                        update("aligning", slot, detail="Verified Ogg ready.", instance_progress=100)
            if (outcome["state"] == "staged" and len(placement_errors) > previous_placement_errors
                    and store_playback is None):
                statuses[-1].update(state="held", reason=placement_errors[-1]["reason"])
            if submit:
                elapsed = round(perf_counter() - dialogue_started, 3)
                npc_seconds[row["npc_id"]] = npc_seconds.get(row["npc_id"], 0) + elapsed
                statuses[-1].update(stage=dialogue_stage(row["source_id"]), already_ready=cached,
                                    elapsed_seconds=elapsed)
            if on_progress:
                on_progress(placed, len(rows))
        if waiting_placement is not None:
            flush_staged()
            if on_progress:
                on_progress(placed, len(rows))
    area_results = []
    for area in areas:
        counts = {key:0 for key in ("staged","queued","waiting_for_base","held","excluded","source_unavailable")}
        for record in statuses:
            if record["area"] == area:
                state = record["state"]
                counts[state if state in counts else "held"] += 1
        area_results.append(dict(area=area, **counts))
    paused = bool(should_stop and should_stop())
    phase, detail = fetch_summary(placed, statuses, placement_errors, paused=paused,
                                  restart_required=delivery.get("restart_required", False),
                                  fallback_reload=bool(disabled_stock_paths))
    update(phase, detail=detail)
    area_coverage = None
    if submit and store_playback is not None and selected_keys and placed:
        area_coverage = coverage.count(area_rows)
    return dict(**delivery, name=name, areas=area_results, statuses=statuses, held=[], paused=paused,
                area_coverage=area_coverage,
                **({"coverage_progress": coverage.snapshot(progress_row)} if coverage is not None else {}),
                retryable_returns=0 if replacement else sum(bool(r.get("retryable_return")) for r in statuses),
                final_delivery_pending=any(item.get('final_delivery_pending') for item in statuses),
                placed_dialogues=placed, placement_errors=placement_errors,
                fallback_reload=bool(disabled_stock_paths),
                placement_pending=len(staged_paths)-placed, pending_upload_bytes=0,
                elapsed_seconds=round(perf_counter()-started,3))
