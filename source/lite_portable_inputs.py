"""Verify explicit plan input and bind it to the user's installed release."""

from base64 import b64decode
from bisect import bisect_left
from collections.abc import Mapping
from collections import OrderedDict
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import RLock
import wave
from zipfile import BadZipFile, ZipFile
from zlib import decompressobj, error as CompressionError

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_der_public_key
import numpy as np
import soundfile as sf

from guide_bindings import RATE
from phoneme_alignment import UNKNOWN_PHONES, bind_transcript, lexical_text, transcript_digest, validate, word_offsets
from lite_portable_constraints import name_fallback_article_changed, CLASS_MASKS, name_occurrences
from lite_portable_io import archive_names_under, cache_bytes, digest, evidence_matches, file_hash, hold
from lite_portable_join import JoinWaveform, expanded, join_windows
from lite_source_identity import dialogue_source_tuple, quest_wording_key, source_key
from lite_stock_text import MAX_TABLE_BYTES, matches_edited_table


PACK_KEY = "MCowBQYDK2VwAyEAtiV14d1ANsreuZtPlaGdosc1ZUyk9QlC7I4KpXjNrA4="
PACK = re.compile(r"SpokenQuestsAudio(?:Alliance|Horde|Shared|Gossip)")
STOCK = re.compile(r"generated/sounds/(?:quests|gossip|followup)/[A-Za-z0-9_-]{1,100}\.ogg")
HASH = re.compile(r"[0-9a-f]{64}")
PACKAGED_RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
COMPANION_ADDONS = {"DMSpeaksSpokenAlliance", "DMSpeaksSpokenHorde", "DMSpeaksSpokenShared", "DMSpeaksSpokenGossip"}
INPUTS = {"plans": ("dmspeaks-lite-plans-1", 8_000_000_000, "dialogues"),
          "returns": ("dmspeaks-lite-returns-1", 48_000_000, "returns")}
SETTING = "lite_verified_inputs"
PLAN_ADMISSION = "lite_verified_plan_build"
STOCK_PCM_REPAIRS_SHA256 = "8e6e9f78d91d358b0dd1089655c367f5b05cf22756848c4c001d656ca3c7ca3f"
PUBLIC_EVIDENCE_MEMBER = "portable-provenance.json"
PUBLIC_EVIDENCE_HASHES = {
    "4745ce5aaa03b245cea65a9e83c43fefba0e04ed32990b02ef8b055bf9ada752",
    "8b16c3f5447ea4ab07da37e33b281e746b218979e054e4dfee6ab12cde25515e",
    "0a02e553395126104a9803e44db6854fead13aced7c6bd1b25a324897a61db02",
    "604e9fffa651d0cfb9f58f06b8865a9b380d05f8a9bc8a54fe412943f9a3339c",
    "c0552899a93d1681aa28a75b7706351c2e87c04de540e25a17afab333c2e85e3",
    "8501509cb88fb56a693eba29bfc55988ae29686bdad5499f1b5ac0507fee0fab",
    "ab8ce4bd39a386dbc103b3eabba4d3277e8f725ca8b50871b10614372e536ac8",
    "7776aa2feede5654d88c0e72fd3e38e6d9df59c310dccb9ff51e6c0e37d60019",
    "0c3c43d285d94169bd30cd855c866b979cf58069aab5ade335c17d3ca547b091",
    "4107f369cea0c529421e99fbbf5021b96ed43e5b18abea2057fc1cc2f577e846",
    "c3e41294a9673880936feaab5b6232b4d114aff3752ab6fdc329bda0847bdbf0",
    "7173fcb661894bfe7b908664b1336256352c123e3575dc8030cce659a30e8ad6",
    "9b957a8d1f4234fd2b1b07c69faeca98abdcc0fd7ae89ea151405731061bda7f",
    "35f349b84da49a507ed66751e001d9403e69eba851ae1e0938bc8f56fe856272",
    "c5552caca368b9ef15c3c76579c57fe168bd6e16c9004aa80cb656ae3a342774",
    "a118efcf72355ada7015845e5b6c22c526818aff71fead7a640c7d80a9c8980a",
    "36bf766b9333d4570f3239cc7c2b48f5d303c1170d05ae3cb383cca3e1914f44",
    "6974e6965ffc86fe96b6a3d88c86329faa9d74905b9ba129cbdc3040d02c9294",
    "748ffc63147abe7099df050936e94c56b65f1aa54beda25214d05f19a4be9a36",
    "185560153370956d4982a46c0bcce0f8fea15b7602cfd3fa588e568b07294dc3",
    "81623545c55c84496e4659594b06d5feea54dd51b2084c613ac4800099c6d8e2",
    "1a211d3904568242ed134f6370144cb3839a6e2d2ae23f6f9a959b5e480c0360",
    "2cb3089322b22be4e3ab1a84ce95fa7627fd576efe0e5a11d100d1ebf28581dd",
    "d934d42ab9ccef4feab86cf6bd8a0c61d8e84f7463e202ae009d2a67e2e6c230",
    "7c7f78e03be500a71e0b6502bf46ad5fa32cfdac49a237d463128f946b3457a3",
    "d4a89fd0d5b127c7fce619c11c0fef325ae36272f685e3ec32a3712465d20374",
    "29c79d31d4aca2648b2fc77e8926c06c51b45306f405a84bcd7f19b36c8139d2",
    "d617888f77e8a95a177227b0f1f883580fd469ef3d5fc753f300f5ecde0139aa",
    "35112f6900fe788ace32100c6832f5aacee7c6aad1d792e2f123eaba720533b3",
}
PORTABLE_DOCUMENT = re.compile(
    r"(?:eligibility|references/contracts)/[a-f0-9]{64}\.json"
    r"|native-capsules/[a-f0-9]{64}/(?:native/evidence/[a-f0-9]{64}\.(?:json|asset)"
    r"|references/contracts/[a-f0-9]{64}\.json)"
)
_PLAN_CACHE = OrderedDict()
_PLAN_CACHE_LOCK = RLock()


class InputImportCancelled(ValueError):
    pass


def import_progress(progress, should_stop, stage, current=0, total=0):
    if should_stop and should_stop():
        raise InputImportCancelled("Dialogue preparation was cancelled.")
    if progress:
        progress(dict(stage=stage, current=current, total=total))


def checked_hash(path, *, progress=None, should_stop=None, stage="checking"):
    result = sha256()
    total, current = path.stat().st_size, 0
    with path.open("rb") as stream:
        while True:
            import_progress(progress, should_stop, stage, current, total)
            block = stream.read(1024 * 1024)
            if not block:
                return result.hexdigest()
            result.update(block)
            current += len(block)


def plan_cache_key(data):
    return (data, *archive_identity(data)) if isinstance(data, Path) else (data,)


def cached_plan(data, expected=None):
    key = plan_cache_key(data)
    with _PLAN_CACHE_LOCK:
        cached = _PLAN_CACHE.get(key)
        if cached is not None and (expected is None or cached[2] == expected):
            _PLAN_CACHE.move_to_end(key)
            return cached[:2]


def remember_plan(data, result, expected=None):
    key = plan_cache_key(data)
    with _PLAN_CACHE_LOCK:
        _PLAN_CACHE[key] = (*result, expected)
        _PLAN_CACHE.move_to_end(key)
        while len(_PLAN_CACHE) > 2:
            _PLAN_CACHE.popitem(last=False)
    return result


def archive_identity(path):
    state = path.stat()
    return state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns


class ArchiveMembers(Mapping):
    """Read catalog media on demand without retaining every decoded ZIP member."""

    def __init__(self, data, limit, max_members):
        self.path = data if isinstance(data, Path) else None
        self.identity = archive_identity(self.path) if self.path else None
        self.archive = ZipFile(data if self.path else BytesIO(data))
        files = self.archive.infolist()
        if sum(file.file_size for file in files) > limit or len(files) > max_members:
            self.archive.close()
            raise ValueError("The Lite input exceeds its supported archive size.")
        self.files = {}
        for file in files:
            name = file.filename
            member_limit = 256_000_000 if name == "input.json" else 24_000_000
            if file.file_size > member_limit:
                self.archive.close()
                raise ValueError("The Lite input contains a member exceeding its supported size.")
            route = PurePosixPath(name)
            if (name in self.files or file.is_dir() or route.is_absolute() or ".." in route.parts
                    or "\\" in name or not re.fullmatch(r"[A-Za-z0-9_./-]+", name)):
                self.archive.close()
                raise ValueError("The Lite input contains an invalid file path.")
            self.files[name] = file
        self.sorted_names = sorted(self.files)
        self.portable_documents = {}
        self.installed_references = {}
        self.companion_joins = {}
        self.omitted_voice_media = frozenset()
        self.native_reference_aliases = {}
        self.name_fetch_holds = {}
        self.verified_name_playback = {}
        self.skyborne_eligibility = {}
        if PUBLIC_EVIDENCE_MEMBER in self.files:
            raw = self[PUBLIC_EVIDENCE_MEMBER]
            if digest(raw) not in PUBLIC_EVIDENCE_HASHES:
                raise ValueError("This version of DMSpeaks Lite cannot verify the installed name data. "
                                 "Install the version of Lite listed for this DMSpeaks add-on release.")
            projection = json.loads(raw)
            if (not isinstance(projection, dict)
                    or set(projection) - {"companion_joins", "omitted_voice_media", "native_reference_aliases",
                                          "name_fetch_holds", "skyborne_eligibility", "verified_name_playback_sha256"}
                    != {"format", "documents", "installed_references"}
                    or projection["format"] != "dmspeaks-portable-provenance-1"
                    or not isinstance(projection["documents"], dict)
                    or not isinstance(projection["installed_references"], dict)):
                raise ValueError("The portable evidence has an unsupported layout.")
            for name, pair in projection["documents"].items():
                if (name not in self.files or not PORTABLE_DOCUMENT.fullmatch(name)
                        or not isinstance(pair, list) or len(pair) != 2
                        or pair[0] != PurePosixPath(name).stem
                        or not all(isinstance(pin, str) and HASH.fullmatch(pin) for pin in pair)
                        or pair[0] == pair[1]):
                    raise ValueError("The portable evidence has an invalid document binding.")
            self.portable_documents = projection["documents"]
            for name, media in projection["installed_references"].items():
                if (name not in self.files or not re.fullmatch(r"references/contracts/[a-f0-9]{64}\.json", name)
                        or not isinstance(media, str) or media in self.files
                        or not re.fullmatch(r"references/media/[a-f0-9]{64}\.flac", media)):
                    raise ValueError("The portable evidence has an invalid installed-reference binding.")
            self.installed_references = projection["installed_references"]
            joins = projection.get("companion_joins", {})
            if not isinstance(joins, dict) or any(
                    not isinstance(seed, str) or not HASH.fullmatch(seed)
                    or not isinstance(checksum, str) or not HASH.fullmatch(checksum)
                    or f"companions/{seed}/joins.json" not in self.files for seed, checksum in joins.items()):
                raise ValueError("The measured playback joins have an invalid source binding.")
            self.companion_joins = joins
            omitted = projection.get("omitted_voice_media", [])
            if not isinstance(omitted, list) or any(
                    not isinstance(media, str) or media in self.files
                    or not re.fullmatch(r"references/media/[a-f0-9]{64}\.flac", media) for media in omitted):
                raise ValueError("The installed-reference catalog has an invalid omitted voice asset.")
            self.omitted_voice_media = frozenset(omitted)
            if len(self.omitted_voice_media) != len(omitted):
                raise ValueError("The installed-reference catalog repeats an omitted voice asset.")
            aliases = projection.get("native_reference_aliases", {})
            if not isinstance(aliases, dict) or any(
                    not re.fullmatch(r"references/media/[a-f0-9]{64}\.flac", name)
                    or name in self.files or not isinstance(target, str) or target not in self.files
                    or not re.fullmatch(r"native-capsules/[a-f0-9]{64}/references/media/[a-f0-9]{64}\.flac", target)
                    or target.split("/", 2)[2] != name for name, target in aliases.items()):
                raise ValueError("An archived reference alias differs from its checked media identity.")
            self.native_reference_aliases = aliases
            if "skyborne_eligibility" in projection:
                self.skyborne_eligibility = read_skyborne_eligibility(
                    projection["skyborne_eligibility"], digest(self["input.json"]))
            if "name_fetch_holds" in projection:
                holds = projection["name_fetch_holds"]
                if (not isinstance(holds, dict) or set(holds) != {"input_sha256", "reasons"}
                        or not isinstance(holds["reasons"], dict) or not holds["reasons"]
                        or holds["input_sha256"] != digest(self["input.json"])
                        or any(not isinstance(reason, str) or not 0 < len(reason) <= 240
                               for reason in holds["reasons"].values())):
                    raise ValueError("The installed name catalog has invalid coverage records.")
                self.name_fetch_holds = holds["reasons"]
            if "verified_name_playback_sha256" in projection:
                raw = self["references/verified-name-playback.json"]
                if digest(raw) != projection["verified_name_playback_sha256"]:
                    raise ValueError("The name playback evidence differs from its verified catalog.")
                proof = json.loads(raw)
                if (not isinstance(proof, dict) or proof.get("format") != "dmspeaks-verified-name-playback-1"
                        or not isinstance(proof.get("plans"), dict)):
                    raise ValueError("The name playback evidence is unreadable.")
                self.verified_name_playback = proof["plans"]

    def projected_evidence(self, name):
        return self.portable_documents.get(name)

    def installed_reference(self, name):
        return self.installed_references.get(name)

    def reference_media(self, name):
        return self[self.native_reference_aliases.get(name, name)]

    def __iter__(self):
        return iter(self.files)

    def names_under(self, prefix):
        start = bisect_left(self.sorted_names, prefix)
        for index in range(start, len(self.sorted_names)):
            name = self.sorted_names[index]
            if not name.startswith(prefix):
                break
            yield name

    def __len__(self):
        return len(self.files)

    def __getitem__(self, name):
        if self.path and archive_identity(self.path) != self.identity:
            raise ValueError("The prepared catalog changed while reading its verified media.")
        try:
            return self.archive.read(self.files[name])
        except (BadZipFile, RuntimeError) as error:
            raise ValueError("Choose an intact Lite input ZIP.") from error

    def __del__(self):
        archive = getattr(self, "archive", None)
        if archive is not None:
            archive.close()


def safe_members(data, limit, *, max_members=500, lazy=False):
    try:
        members = ArchiveMembers(data, limit, max_members)
        if not lazy:
            members = dict(members.items())
    except (BadZipFile, RuntimeError) as error:
        raise ValueError("Choose an intact Lite input ZIP.") from error
    return members


def verify_release(raw):
    envelope = json.loads(raw)
    try:
        content = envelope["signed"].encode()
        key = load_der_public_key(b64decode(PACK_KEY))
        key.verify(b64decode(envelope["sig"], validate=True), content)
    except (InvalidSignature, KeyError, TypeError, ValueError) as error:
        raise ValueError("Spoken's release signature is invalid.") from error
    return digest(content), json.loads(content)


SIGNED_QUEST_SUFFIXES = {
    ("q:257:complete", 1187): "ama",
    ("q:258:accept", 1187): "ath",
    ("q:258:progress", 1187): "ah",
}


def verify_name_slots(row):
    required = {"source_cut", "context", "slot", "leading_word"}
    optional = {"request_context", "class_articles", "race_articles"}
    if "name_occurrence" in row:
        raise ValueError("Name occurrence numbers are derived from their source order.")
    if "name_slots" in row:
        slots = row["name_slots"]
        if ("companion_seed" not in row or not isinstance(slots, list) or len(slots) < 2
                or row.keys() & (required | optional)
                or any(not isinstance(slot, dict) or not required <= slot.keys()
                       or not slot.keys() <= required | optional
                       for slot in slots)):
            raise ValueError("Repeated names require a verified companion and separate Name slots.")
    previous_sample = previous_char = 0
    text = row["stock_text"]
    for occurrence in name_occurrences(row):
        start, end = occurrence["source_cut"]
        lo, hi = occurrence["context"]
        left, right = occurrence["slot"]
        whole_name_phrase = ("companion_seed" in row and ("native_source" in row
                             or (occurrence.get("leading_word") is not None
                                 and ("analysis_base" in row or row.get("requires_paced_base") is True))))
        if (not previous_sample <= start < end <= row["samples"]
                or ((start, end) == (0, row["samples"]) and not whole_name_phrase)
                or not 0 <= lo <= left < right <= hi <= len(text)
                or left < previous_char):
            raise ValueError("The prepared Name slot or sample bounds are invalid.")
        token = text[left:right]
        marked_name = (token == "N" and (left == 0 or not re.match(r"[\w$]", text[left - 1]))
                       and (right == len(text) or not re.match(r"\w", text[right])))
        if token.casefold() != "adventurer" and not marked_name:
            raise ValueError("The prepared Name slot needs Adventurer or a standalone N marker.")
        previous_sample, previous_char = end, right
        signed_followup = row.get("requires_paced_base") is True and row.get("source_id", "").startswith("f:")
        quest_suffix = SIGNED_QUEST_SUFFIXES.get((row.get("source_id"), row.get("npc_id")))
        suffix_pattern = r"['’]s(?!\w)" if signed_followup else None
        if quest_suffix and row.get("requires_paced_base") is True and "companion_seed" in row:
            suffix_pattern = re.escape(quest_suffix) + r"(?!\w)"
        attached = re.match(suffix_pattern, row["stock_text"][right:hi]) if suffix_pattern else None
        suffix = occurrence.get("name_suffix")
        if ((suffix is not None or attached)
                and (not attached or suffix != attached[0] or "name_slots" in row or row.get("expanded_name_binding"))):
            raise ValueError("A prepared Name suffix needs its exact signed dialogue and attached spelling.")
        leading = occurrence.get("leading_word")
        if leading is not None:
            if (not isinstance(leading, dict)
                    or set(leading) != {"source_chars", "source_text", "spoken_text",
                                       "source_word_index", "return_word_index"}):
                raise ValueError("The prepared Name needs its measured leading-word binding.")
            before, after = leading["source_chars"]
            if (type(before) is not int or type(after) is not int or not lo <= before < after <= left
                    or leading["source_text"] != row["stock_text"][before:after]
                    or (leading["source_text"].casefold() != leading["spoken_text"].casefold()
                        and not (row.get("requires_paced_base") is True and "companion_seed" in row
                                 and name_fallback_article_changed(occurrence)))
                    or any(type(leading[key]) is not int or leading[key] < 0
                           for key in ("source_word_index", "return_word_index"))):
                raise ValueError("The prepared leading word differs from the source Name context.")
        request_context = occurrence.get("request_context")
        stock_context = row["stock_text"][lo:hi]
        if request_context is not None and (
                not isinstance(request_context, str) or len(request_context) != len(stock_context)
                or request_context.casefold() != stock_context.casefold()):
            raise ValueError("The prepared request context differs from the signed transcript.")
        article_spans = []
        for kind in ("class", "race"):
            key = kind + "_articles"
            if key not in occurrence:
                continue
            articles = occurrence[key]
            if "companion_seed" not in row or not isinstance(articles, list):
                raise ValueError(f"The prepared {kind} articles need a verified companion.")
            previous_end = lo
            for article in articles:
                if (not isinstance(article, dict) or set(article) != {"source_chars", "spoken_text", "occurrence"}
                        or not isinstance(article["source_chars"], list) or len(article["source_chars"]) != 2
                        or not isinstance(article["spoken_text"], str)):
                    raise ValueError(f"The prepared {kind} article is malformed.")
                start, end = article["source_chars"]
                if (type(start) is not int or type(end) is not int or not previous_end <= start < end <= hi
                        or not (end <= left or start >= right)
                        or {row["stock_text"][start:end].casefold(), article["spoken_text"].casefold()} != {"a", "an"}
                        or type(article["occurrence"]) is not int or article["occurrence"] < 1):
                    raise ValueError(f"A prepared {kind} article does not match its source occurrence.")
                previous_end = end
                article_spans.append((start, end))
        article_spans.sort()
        if any(end > next_start for (_, end), (next_start, _) in zip(article_spans, article_spans[1:])):
            raise ValueError("The prepared class and race articles overlap.")


def paced_quest_route(row, members):
    """Bind a plan's runtime quest selector to its exact bundled playback seed."""
    seed = row.get("companion_seed")
    measured_stock_base = row.get("reference_from_stock") is True and row.get("paced_base_analysis")
    if ((row.get("requires_paced_base") is not True and not measured_stock_base)
            or row.get("native_source") or not seed):
        raise ValueError("Shared quest references need separately verified paced playback seeds.")
    expected = seed["manifest_sha256"]
    content = members[f"companions/{expected}/dialogue.json"]
    if digest(content) != expected:
        raise ValueError("The paced quest variant seed changed.")
    source = json.loads(content)
    selector = dialogue_source_tuple(source)
    text = source.get("original_text")
    wording = quest_wording_key(source)
    if (selector != dialogue_source_tuple(row) or wording is None
            or digest(text.encode()) != row.get("source_text_sha256")):
        raise ValueError("The paced quest variant seed belongs to different quest wording.")
    if row.get("quest_variant"):
        analysis = row["analysis_base"]
        if (text, source.get("source_wave_sha256"), source.get("source_samples"),
                source.get("spoken_key"), source.get("original_spoken")) != (
                analysis["quest_text"], analysis["wave_sha256"], row["samples"], row["key"], None):
            raise ValueError("The paced quest variant differs from its reconstructed recording.")
    return (*selector, wording)


def read_skyborne_eligibility(binding, input_hash):
    if (not isinstance(binding, dict) or set(binding) != {"input_sha256", "rows"}
            or binding["input_sha256"] != input_hash or not isinstance(binding["rows"], dict)):
        raise ValueError("The Skyborne eligibility data belongs to a different name catalog.")
    for entry in binding["rows"].values():
        if not isinstance(entry, dict) or set(entry) != {"variants", "required_classes_mask"}:
            raise ValueError("The Skyborne eligibility data has an invalid entry.")
        variants = entry["variants"]
        mask = entry["required_classes_mask"]
        if (not isinstance(variants, list) or not 1 <= len(variants) <= 2
                or any(v not in ("high order skyborne", "windshaper skyborne") for v in variants)
                or len(set(variants)) != len(variants) or type(mask) is not int or mask < 0):
            raise ValueError("The Skyborne eligibility data has invalid race or class restrictions.")
    return binding["rows"]


def gendered_stock_revision(old, alternate):
    """Identify a signed quest recording whose filename selects a player sex."""
    original = PurePosixPath(old["stock_path"])
    actual = alternate.get("stock_path")
    if not old["source_id"].startswith("q:") or old.get("quest_variant") or not isinstance(actual, str):
        return False
    return any(actual == original.with_name(prefix + original.name).as_posix()
               and alternate.get("sexes") == [gender]
               and (old.get("sexes") is None or gender in old["sexes"])
               for prefix, gender in (("m-", "male"), ("f-", "female")))


def equivalent_gendered_wording(old, alternate, texts):
    """Check that the new authored wording changes only the existing gender term."""
    if (not isinstance(texts, list) or len(texts) != 2
            or not all(isinstance(value, str) for value in texts)):
        return False
    previous, revised = texts
    if (digest(previous.encode()) != old["source_text_sha256"]
            or digest(revised.encode()) != alternate.get("source_text_sha256")):
        return False
    sex = alternate["sexes"][0]
    branches = re.compile(r"\$[Gg]([^$:;]+):([^$:;]+);")

    def wording(text, gender):
        selected = branches.sub(lambda match: match[1 if gender == "male" else 2], text)
        return " ".join(re.sub(r"\$[Bb]", " ", selected).split())

    # Older single recordings may use the male form as the canonical text.
    # The newer, explicit gender token must resolve to that wording in the male branch.
    if branches.search(previous):
        return all(wording(previous, gender) == wording(revised, gender) for gender in ("male", "female"))
    return (sex == "female" and branches.search(revised) is not None
            and wording(previous, "male") == wording(revised, "male"))


def stock_item_giver_revision(old, row, release):
    """Recognize the reviewed Corrupted Totems change of dialogue owner."""
    return (
        release == "b0a034463a891e3e21ff8181d36b73b183d3e74894d2e90a621dbdb34b8d9628"
        and old["source_id"] == "q:79366:accept"
        and old.get("npc_id") == 5905
        and old.get("giver_type", "Creature") == "Creature"
        and old["stock_sha256"] == "41b6837c6ffa10b168366f91a704e5ddfb3676029a2e16b6cef2ef419055fb6f"
        and row.get("npc_id") == 268582
        and row.get("giver_type") == "Item"
        and row.get("stock_sha256") == "a4c272323f4751d058286816134a6985d610074812954e401174e4ea3554756c"
    )


_DEFIAS_FOLLOWUP_BASE = "SpokenQuestsAudioAlliance/generated/sounds/followup/"
_DEFIAS_FOLLOWUP_PRIOR = _DEFIAS_FOLLOWUP_BASE + "9-human-male-standard.ogg"
_DEFIAS_FOLLOWUP_PUBLISHER = _DEFIAS_FOLLOWUP_BASE + "9-human-male-standard-human-male-warrior.ogg"
_DEFIAS_FOLLOWUP_LOOKUPS = {
    "followup_lookups.lua": "89906ee1cec19e7dd36c8685e9d11bbd5c63b33db7c0f2b4bb4ca4d3535ff70b",
    "npc_body_followup_lookups.lua": "f4f22a401caeb30c7b15044073db7988ad8c2c5d26d02ac176169d12b74a8f7c",
    "npc_sex_followup_lookups.lua": "3a00ecbaa667d60054050ab7870bdf642f151d35f4f99cd4d6364b003feaabbc",
    "sound_length_table.lua": "61fb2a2e19dc144920e504833661e355d4584b64919f409b204c7d5454d7b1c7",
}


def defias_followup_revision(old, row, release):
    """Admit the signed filename change for the existing Defias follow-up."""
    return (
        release == "b0a034463a891e3e21ff8181d36b73b183d3e74894d2e90a621dbdb34b8d9628"
        and old["source_id"] == "f:9:human-male-standard"
        and old.get("npc_id") == 467
        and old["stock_path"] == _DEFIAS_FOLLOWUP_PRIOR
        and old["stock_sha256"] == "547d6653e10802118b9e85fcbaa1ae8aec6a99f4465f3c89616081daea3544f8"
        and row.get("stock_path") == _DEFIAS_FOLLOWUP_PUBLISHER
        and row.get("stock_sha256") == "d9741bd777e78a688178b7c0610721ae8469ae3efd1875b5a9ae74f5fe9b939f"
        and row.get("sexes") == old.get("sexes")
    )


def stock_revision_rows(manifest, rows):
    variants = manifest.get("stock_revisions", {})
    if not variants:
        if not isinstance(variants, dict):
            raise ValueError("Spoken stock revisions must be grouped by dialogue source.")
        return []
    primary = {source_key(row): row for row in rows}
    if not isinstance(variants, dict) or not variants.keys() <= primary.keys():
        raise ValueError("A Spoken stock revision has no original dialogue plan.")

    records = []
    scope = ("source_id", "locale", "races", "classes",
             "minimum_level", "npc_aliases", "quest_eligibility",
             "quest_variant", "game_object_reference")
    for key, alternatives in variants.items():
        old = primary[key]
        if old.get("native_source") or not isinstance(alternatives, list) or not alternatives:
            raise ValueError("Spoken stock revisions require an original stock recording.")
        digests = {old["stock_sha256"]}
        for variant in alternatives:
            if not isinstance(variant, dict) or variant.keys() not in (
                    {"release", "row"}, {"release", "row", "source_texts"}):
                raise ValueError("A Spoken stock revision needs its signed release and complete plan.")
            release, row = variant["release"], variant["row"]
            if (not isinstance(release, str) or not HASH.fullmatch(release) or not isinstance(row, dict)
                    or row.get("native_source") or "catalog_source_key" in row
                    or any(row.get(field) != old.get(field) for field in scope)):
                raise ValueError("A Spoken stock revision changed its dialogue or character scope.")
            if ((row.get("npc_id"), row.get("giver_type")) != (old.get("npc_id"), old.get("giver_type"))
                    and not stock_item_giver_revision(old, row, release)):
                raise ValueError("A Spoken stock revision changed its dialogue owner.")
            split = gendered_stock_revision(old, row)
            if (not split
                    and (row.get("stock_path"), row.get("sexes")) != (old["stock_path"], old.get("sexes"))
                    and not defias_followup_revision(old, row, release)):
                raise ValueError("A Spoken stock revision changed its recording path or character scope.")
            same_text = row.get("source_text_sha256") == old["source_text_sha256"]
            if ((same_text and "source_texts" in variant)
                    or not same_text and not (split and equivalent_gendered_wording(old, row, variant.get("source_texts")))):
                raise ValueError("A Spoken stock revision changed its dialogue wording.")
            stock = row.get("stock_sha256")
            if not isinstance(stock, str) or not HASH.fullmatch(stock) or stock in digests:
                raise ValueError("A Spoken stock revision needs a distinct recording checksum.")
            digests.add(stock)
            records.append((release, row))
    return records


def verified_rows(members, manifest, *, progress=None, should_stop=None):
    _, index = verify_release(members["signed-index.json"])
    releases = {}
    indexes = {None: index}
    paths = set()
    by_reference = {}
    rows = manifest["dialogues"]
    if any("catalog_source_key" in row for row in rows):
        raise ValueError("A catalog row cannot override its dialogue identity.")
    skyborne = getattr(members, "skyborne_eligibility", {})
    if skyborne and not skyborne.keys() <= {source_key(row) for row in rows}:
        raise ValueError("A Skyborne eligibility record refers to an unknown dialogue.")
    if holds := getattr(members, "name_fetch_holds", {}):
        keyed = {source_key(row): row for row in rows}
        if len(keyed) != len(rows) or not holds.keys() <= keyed.keys():
            raise ValueError("The installed name catalog has unmatched coverage records.")
        rows = [row for key, row in keyed.items() if key not in holds]

    revisions = stock_revision_rows(manifest, rows)
    records = [(None, row) for row in rows] + revisions

    def report_native_sources(step):
        progress({**step, "catalog_row": ordinal})

    for ordinal, (release, row) in enumerate(records):
        import_progress(progress, should_stop, "verifying", ordinal, len(records))
        if "signed_stock_caption" in row and (
                not isinstance(row["signed_stock_caption"], str) or not row["signed_stock_caption"].strip()
                or row.get("native_source") or not row.get("companion_seed") or not row.get("reference_contract")):
            raise ValueError("A separate signed caption needs its verified playback and speech reference.")
        if "reference_from_stock" in row and (
                row["reference_from_stock"] is not True or "companion_seed" not in row
                or "reference_contract" not in row
                or "native_source" in row or "expanded_name_binding" in row):
            raise ValueError("An installed reference needs a paired signed playback source.")
        if "signed_reference_parent" in row:
            verify_signed_reference_parent(row)
        if "stock_reference_slice" in row:
            verify_stock_reference_slice(row)
        if "stock_pcm_repair" in row:
            if row.get("native_source"):
                raise ValueError("A stock decoder correction requires signed Spoken audio.")
            verify_stock_pcm_repair(stock_decode_contract(row))
        if "quest_variant" in row and (row["quest_variant"] is not True or "analysis_base" not in row
                                       or row.get("reference_from_stock") is not True
                                       or row.get("requires_paced_base") is not True):
            raise ValueError("A quest variant needs its own reconstructed playback and a shared installed reference.")
        if "fish_accent_prefix" in row and "reference_contract" not in row:
            raise ValueError("The prepared Fish accent needs its ordered reference contract.")
        if "requires_paced_base" in row:
            seed = row.get("companion_seed")
            if (row["requires_paced_base"] is not True or "native_source" in row
                    or ("name_slots" in row and "analysis_base" not in row and "paced_base_analysis" not in row)
                    or not isinstance(seed, dict)
                    or not isinstance(seed.get("manifest_sha256"), str)
                    or not HASH.fullmatch(seed["manifest_sha256"])
                    or "reference_contract" not in row or "request_reference_evidence" not in row
                    or f"companions/{seed['manifest_sha256']}/dialogue.json" not in members):
                raise ValueError("This Name plan needs its signed source, ordered references and bundled paced seed.")
        if "game_object_reference" in row and "reference_contract" not in row:
            raise ValueError("A game object reconstruction needs its signed dialogue reference contract.")
        if "reference_contract" in row:
            from lite_portable_references import contract_member, verify_catalog_references

            references = verify_catalog_references(row, members)
            if "signed_stock_caption" in row:
                analysis = row.get("analysis_base")
                installed_original = (not analysis and len(references) == 1 and members.installed_reference(
                    contract_member(row["reference_contract"]["sha256"])))
                if row.get("recorded_stock_opening") or installed_original:
                    spoken = recorded_stock_transcript(row)
                    expected_parts = 1
                else:
                    spoken = analysis["reference"]["transcript"] if analysis else expanded(row["stock_text"])
                    expected_parts = 1 if row.get("game_object_reference") else 2
                if (len(references) != expected_parts or references[-1]["id"] != f"{row['source_id']}:dialogue"
                        or lexical_text(expanded(references[-1]["transcript"])) != lexical_text(expanded(spoken))):
                    raise ValueError("The separate signed caption has a different verified speech transcript.")
            if (row.get("requires_paced_base") and "analysis_base" not in row
                    and references[-1]["wav_bytes"] is not None):
                exact_paced_stock_wave(row, references)
        if "minimum_level" in row and (type(row["minimum_level"]) is not int
                or row["minimum_level"] < 1 or not row["source_id"].startswith("g:")
                or "native_source" in row):
            raise ValueError("The prepared gossip dialogue has an invalid level requirement.")
        if "native_source" in row:
            from lite_native_sources import verify_row
            verify_row(row, members, progress=report_native_sources if progress else None, should_stop=should_stop)
            key = (release, source_key(row))
            if key in paths:raise ValueError("The native source has a duplicate preparation identity.")
            paths.add(key)
            verify_name_slots(row)
            if "paced_base_analysis" in row:
                read_paced_base_analysis(row, members)
            continue
        verify_owners(row, members)
        if "quest_eligibility" in row:
            verify_quest_eligibility(row, members)
        if "expanded_name_binding" in row:
            from lite_expanded_binding import descriptor
            descriptor(row, members)
        pack, _, relative = row["stock_path"].partition("/")
        key = (release, source_key(row))
        if (not PACK.fullmatch(pack) or not STOCK.fullmatch(relative)
                or key in paths or row["key"] != PurePosixPath(relative).stem
                or row["locale"] != "en-US"):
            raise ValueError("The prepared dialogue has an unsupported or duplicate stock binding.")
        paths.add(key)
        by_reference.setdefault((release, row["stock_path"]), []).append(row)
        for field, allowed in (("classes", CLASS_MASKS), ("sexes", ("male", "female"))):
            if field in row and (not isinstance(row[field], list) or not row[field]
                                 or any(not isinstance(value, str) or value not in allowed for value in row[field])
                                 or len(set(row[field])) != len(row[field])):
                raise ValueError("The prepared dialogue has invalid character requirements.")
        identity = (release, pack)
        if identity not in releases:
            prefix = f"spoken-releases/{release}/" if release else ""
            if release not in indexes:
                actual, alternate = verify_release(members[prefix + "index.json"])
                if actual != release:
                    raise ValueError("The alternate Spoken release index changed.")
                indexes[release] = alternate
            signed_hash, signed = verify_release(members[f"{prefix}packs/{pack}.json"])
            revision = indexes[release]["packs"][pack]
            if (signed_hash, signed["rev"], signed["packId"]) != (
                    revision["manifestSha256"], revision["rev"], pack):
                raise ValueError("The Spoken pack differs from its signed release index.")
            releases[identity] = {file["path"]: file for file in signed["files"]}
        signed = releases[identity].get(relative)
        table = releases[identity].get("generated/text_table.lua")
        if (not signed or signed["sha256"] != row["stock_sha256"]
                or not table or table["sha256"] != row["text_table_sha256"]):
            raise ValueError("A prepared dialogue differs from its signed stock release.")
        if not HASH.fullmatch(row["pcm_sha256"]) or not 0 < row["samples"] <= RATE * 120:
            raise ValueError("The prepared dialogue needs a bounded mono sample clock.")
        evidence = row["base_phones"]
        analysis = row.get("analysis_base")
        if "analysis_base" in row:
            verify_analysis_base(row, analysis, members, references=references
                                 if row.get("requires_paced_base") or row.get("reference_from_stock") else None)
        validate(evidence, {"sha256": (analysis or {}).get("wave_sha256", row["stock_wav_sha256"]), "sample_rate": RATE,
                            "sample_count": row["samples"]}, expanded(row["stock_text"]).strip())
        if "paced_base_analysis" in row:
            read_paced_base_analysis(row, members)
        if "companion_seed" in row:
            seed = row["companion_seed"]
            if (not isinstance(seed, dict) or set(seed) != {"addon", "manifest_sha256"}
                    or seed["addon"] not in COMPANION_ADDONS or not HASH.fullmatch(seed["manifest_sha256"])):
                raise ValueError("The prepared mixed dialogue is missing a verified companion binding.")
        verify_name_slots(row)
    for group in by_reference.values():
        routes = {paced_quest_route(row, members) for row in group if len(group) > 1 or row.get("quest_variant")}
        if len(group) > 1 and len(routes) != len(group):
            raise ValueError("Two Name plans sharing a reference also share the same quest wording.")
    if not paths:
        raise ValueError("The verified input contains no prepared dialogues.")
    seeds = {row["companion_seed"]["manifest_sha256"]
             for _, row in records if "companion_seed" in row}
    bundled = {PurePosixPath(name).parts[1] for name in archive_names_under(members, "companions/")
               if len(PurePosixPath(name).parts) > 1}
    if not bundled <= seeds:
        raise ValueError("The prepared catalog includes a playback seed without a dialogue binding.")
    if not getattr(members, "companion_joins", {}).keys() <= bundled:
        raise ValueError("The measured joins have no prepared playback seed.")
    if bundled:
        from lite_portable_companion import validate_bundled_companion

        for ordinal, (_, row) in enumerate(records):
            import_progress(progress, should_stop, "playback", ordinal, len(records))
            if row.get("companion_seed", {}).get("manifest_sha256") in bundled:
                validate_bundled_companion(row, members)


def verify_quest_eligibility(row, members):
    binding = row["quest_eligibility"]
    if (not isinstance(binding, dict) or set(binding) != {
            "member", "required_races_mask", "required_classes_mask", "additional_conditions"}
            or not re.fullmatch(r"eligibility/[0-9a-f]{64}\.json", binding["member"])):
        raise ValueError("The prepared quest needs its reviewed eligibility authority.")
    member = binding["member"]
    raw = members[member]
    if not evidence_matches(members, member, raw, PurePosixPath(member).stem):
        raise ValueError("The reviewed quest eligibility authority changed.")
    authority = json.loads(raw)
    accepted = authority["recommended_overrides"].get(row["source_id"])
    if accepted is None:
        raise ValueError("The source quest has no independently admitted eligibility record.")
    if ((accepted["ordinal"], accepted["source_id"], accepted["npc_id"], accepted["raw_source_sha256"],
         accepted["stock_pack"]) !=
            (row["catalog_ordinal"], row["source_id"], row["npc_id"], row["source_text_sha256"],
             row["stock_path"].split("/", 1)[0])
            or any(binding[key] != accepted[key] for key in
                   ("required_races_mask", "required_classes_mask", "additional_conditions"))
            or not row["races"] or not set(row["races"]) <= set(accepted["current_supported_races"])
            or row.get("classes") != accepted["classes"] or row.get("sexes") != accepted["dialogue_sexes"]):
        raise ValueError("The prepared quest differs from its reviewed race, class or source restrictions.")


def verify_owners(row, members):
    if "catalog_ordinal" in row and (type(row["catalog_ordinal"]) is not int or row["catalog_ordinal"] < 0):
        raise ValueError("The prepared dialogue has an invalid corpus owner.")
    aliases = row.get("npc_aliases", [])
    if not isinstance(aliases, list):
        raise ValueError("The prepared dialogue needs declared corpus aliases.")
    if not aliases:
        return
    member = row.get("ownership_manifest", "")
    if not re.fullmatch(r"ownership/[0-9a-f]{64}\.json", member):
        raise ValueError("The prepared aliases need their checked source manifest.")
    content = members[member]
    if digest(content) != PurePosixPath(member).stem:
        raise ValueError("The checked corpus ownership manifest changed.")
    source = json.loads(content)
    analysis = row.get("analysis_base")
    if (source.get("catalog_ordinal"), source.get("source_id"), source.get("npc_id"),
            source.get("npc_aliases"), source.get("source_wave_sha256"), source.get("original_spoken")) != (
            row["catalog_ordinal"], row["source_id"], row["npc_id"], aliases,
            (analysis or {}).get("wave_sha256", row["stock_wav_sha256"]),
            None if analysis else {"path": row["stock_path"], "sha256": row["stock_sha256"]}):
        raise ValueError("The prepared aliases differ from their checked recording owner.")
    ordinals = {row["catalog_ordinal"]}
    for alias in aliases:
        if (not isinstance(alias, dict) or not {"source_id", "ordinal", "npc_id", "npc"} <= alias.keys()
                or not alias.keys() <= {"source_id", "ordinal", "npc_id", "npc", "source_text_sha256", "matching_basis"}
                or alias["source_id"] != row["source_id"] or type(alias["ordinal"]) is not int
                or alias["ordinal"] < 0 or alias["ordinal"] in ordinals
                or type(alias["npc_id"]) is not int or alias["npc_id"] <= 0
                or not isinstance(alias["npc"], str) or not alias["npc"]):
            raise ValueError("The checked corpus alias has an invalid identity.")
        if (("source_text_sha256" in alias and not HASH.fullmatch(alias["source_text_sha256"]))
                or ("matching_basis" in alias and
                    (not isinstance(alias["matching_basis"], str) or not alias["matching_basis"]))):
            raise ValueError("The checked editorial alias has invalid source provenance.")
        ordinals.add(alias["ordinal"])


def verify_analysis_base(row, analysis, members, *, references=None):
    if (not isinstance(analysis, dict)
            or set(analysis) != {"member", "sha256", "wave_sha256", "seed_variant", "reference", "quest_text"}
            or analysis["member"] not in {f"analysis/{row['stock_sha256']}.flac",
                                          f"analysis/{analysis.get('wave_sha256')}.flac"}
            or not all(HASH.fullmatch(analysis[field]) for field in ("sha256", "wave_sha256"))
            or not re.fullmatch(r"[a-f0-9]{20}", analysis["seed_variant"])
            or not re.fullmatch(r"q:[0-9]+:(accept|progress|complete)(?::[mf])?", row["source_id"])
            or not isinstance(analysis["quest_text"], str) or not 0 < len(analysis["quest_text"]) <= 10000
            or "companion_seed" not in row):
        raise ValueError("The reconstructed base needs its checked analysis recording and playback seed.")
    reference = analysis["reference"]
    if (not isinstance(reference, dict) or set(reference) != {"transcript", "samples", "pcm_sha256"}
            or not isinstance(reference["transcript"], str) or not 0 < len(reference["transcript"]) <= 10000
            or not HASH.fullmatch(reference["pcm_sha256"]) or type(reference["samples"]) is not int
            or not 0 < reference["samples"] <= RATE * 120):
        raise ValueError("The reconstructed dialogue needs a checked signed speech reference.")
    if references is not None:
        from lite_portable_references import signed_source_needed, wave_data

        expected_parts = 1 if row.get("game_object_reference") else 2
        if len(references) != expected_parts:
            raise ValueError("This reconstructed Name base lacks its required voice or dialogue reference.")
        dialogue = references[-1]
        if dialogue["wav_bytes"] is None:
            if (not signed_source_needed(row, members)
                    or (dialogue["sha256"], dialogue["transcript"]) != (
                        row["stock_wav_sha256"], reference["transcript"])):
                raise ValueError("The deferred dialogue reference differs from the checked signed recording.")
        else:
            expected = (row["stock_text"], row["pcm_sha256"]) if dialogue["sha256"] == analysis["wave_sha256"] else (
                reference["transcript"], reference["pcm_sha256"])
            if (dialogue["transcript"], digest(wave_data(dialogue["wav_bytes"])[2])) != expected:
                raise ValueError("The dialogue reference differs from the checked signed or generated recording.")
    analysis_pcm(row, members)
    caption_word_samples(row)


def analysis_pcm(row, members):
    analysis = row["analysis_base"]
    data = members[analysis["member"]]
    if digest(data) != analysis["sha256"]:
        raise ValueError("The reconstructed analysis recording differs from the checked plan.")
    try:
        with sf.SoundFile(BytesIO(data)) as source:
            if (source.format, source.subtype, source.samplerate, source.channels, source.frames) != (
                    "FLAC", "PCM_16", RATE, 1, row["samples"]):
                raise ValueError("The reconstructed analysis recording has the wrong sample clock.")
            pcm = source.read(dtype="int16")
    except sf.LibsndfileError as error:
        raise ValueError("The reconstructed analysis recording is unreadable.") from error
    sample_checked(pcm, RATE, row)
    if digest(wave_bytes(pcm)) != analysis["wave_sha256"]:
        raise ValueError("The reconstructed analysis PCM differs from the aligned waveform.")
    return pcm


def read_paced_wave(prefix, descriptor, members):
    encoding = descriptor.get("wave_encoding", "wav")
    if encoding == "wav":
        return members[f"{prefix}.wav"]
    if encoding != "zlib":
        raise ValueError("The measured paced waveform uses an unsupported encoding.")
    # Float PCM uses four bytes per frame. Bound the additional WAV chunks.
    limit = descriptor["samples"] * 4 + 65_536
    inflater = decompressobj()
    try:
        wave_raw = inflater.decompress(members[f"{prefix}.wav.zlib"], limit + 1)
    except CompressionError as error:
        raise ValueError("The compressed measured paced waveform is invalid.") from error
    if len(wave_raw) > limit or not inflater.eof or inflater.unused_data:
        raise ValueError("The compressed measured paced waveform is truncated or exceeds its sample contract.")
    return wave_raw


def bind_paced_transcript(phones, identity, measured, transcript, source, homophones):
    validate(phones, identity, measured)
    old, current = phones["words"], source["words"]
    if len(old) != len(current):
        raise ValueError("The measured paced analysis changed lexical ownership.")
    changed = [index for index, (first, second) in enumerate(zip(old, current, strict=True))
               if lexical_text(first["word"]) != lexical_text(second["word"])]
    if (not isinstance(homophones, list) or any(type(index) is not int for index in homophones)
            or homophones != changed):
        raise ValueError("The measured paced analysis changed lexical ownership.")
    if not changed:
        return bind_transcript(phones, identity, measured, transcript)["words"]
    if any(phones[key] != source[key] for key in ("language", "phone_set")):
        raise ValueError("The measured homophones use different pronunciation alphabets.")
    for index in changed:
        measured_phones = [phone["phone"] for phone in old[index]["phonemes"]]
        source_phones = [phone["phone"] for phone in current[index]["phonemes"]]
        if (not measured_phones or any(phone.casefold() in UNKNOWN_PHONES for phone in measured_phones)
                or measured_phones != source_phones):
            raise ValueError("The measured spelling differs in pronunciation.")
    labels = [word["word"] for word in current]
    offsets = word_offsets(transcript, labels)
    words = [dict(word, word=label, char_start=left, char_end=right)
             for word, label, (left, right) in zip(old, labels, offsets, strict=True)]
    rebound = {**phones, "words": words, "transcript_sha256": transcript_digest(transcript)}
    return validate(rebound, identity, transcript)


def read_paced_join_windows(prefix, descriptor, row, words, members):
    evidence = descriptor["join_windows"]
    frames = descriptor["samples"]
    slots = name_occurrences(row)
    required = {"pcm_sha256", "spans", "windows"}
    if (not isinstance(evidence, dict) or not required <= evidence.keys() <= required | {"leading_word_counts"}
            or not isinstance(evidence["pcm_sha256"], str) or not HASH.fullmatch(evidence["pcm_sha256"])
            or not isinstance(evidence["spans"], list) or len(evidence["spans"]) != len(slots)
            or any(not isinstance(span, list) or len(span) != 2
                   or any(type(point) is not int for point in span)
                   or not 0 <= span[0] < span[1] <= frames for span in evidence["spans"])
            or ("leading_word_counts" in evidence and (
                not isinstance(evidence["leading_word_counts"], list)
                or len(evidence["leading_word_counts"]) != len(slots)))
            or evidence["windows"] != join_windows(words, row, evidence["spans"], frames,
                                                    evidence.get("leading_word_counts"))):
        raise ValueError("The measured join windows do not cover the declared Name cuts.")
    limit = 4 * sum(end - start for start, end in evidence["windows"])
    inflater = decompressobj()
    try:
        data = inflater.decompress(members[f"{prefix}.{evidence['pcm_sha256']}.joins.f32.zlib"], limit + 1)
    except CompressionError as error:
        raise ValueError("The measured join waveform is unreadable.") from error
    if (len(data) != limit or not inflater.eof or inflater.unused_data
            or digest(data) != evidence["pcm_sha256"]):
        raise ValueError("The measured join waveform differs from its source binding.")
    pcm = np.frombuffer(data, dtype="<f4")
    if not np.isfinite(pcm).all():
        raise ValueError("The measured join waveform contains invalid samples.")
    segments = []
    offset = 0
    for start, end in evidence["windows"]:
        count = end - start
        segments.append((start, pcm[offset:offset + count]))
        offset += count
    return JoinWaveform(segments, frames)


def read_paced_base_analysis(row, members):
    """Read the measured source clock and waveform for paced joins."""
    descriptor = row["paced_base_analysis"]
    transcript = expanded(row["stock_text"]).strip()
    required = {"unit", "samples", "wave_sha256", "phones_sha256"}
    mixed_seed = (row.get("companion_seed") and row.get("reference_contract")
                  and not row.get("expanded_name_binding"))
    if (not isinstance(descriptor, dict)
            or (row.get("requires_paced_base") is not True
                and (not mixed_seed or "join_windows" not in descriptor))
            or not required <= descriptor.keys() <= required | {
                "wave_encoding", "measured_transcript", "join_windows", "homophones", "clock_transcript"}
            or ("join_windows" in descriptor and "wave_encoding" in descriptor)
            or type(descriptor["samples"]) is not int or not 0 < descriptor["samples"] <= RATE * 120
            or any(not isinstance(descriptor[field], str) or not HASH.fullmatch(descriptor[field])
                   for field in ("wave_sha256", "phones_sha256"))):
        raise ValueError("The measured paced analysis does not own this source recording.")
    measured_transcript = descriptor.get("measured_transcript", transcript)
    if not isinstance(measured_transcript, str) or not 0 < len(measured_transcript) <= 24_000:
        raise ValueError("The measured paced analysis changed the source transcript.")
    clock_transcript = descriptor.get("clock_transcript", measured_transcript)
    if "clock_transcript" in descriptor and (
            not isinstance(clock_transcript, str) or not 0 < len(clock_transcript) <= 24_000
            or clock_transcript == measured_transcript or measured_transcript != transcript
            or row.get("requires_paced_base") is not True or not row.get("stock_path")
            or not row.get("companion_seed") or row.get("native_source") or row.get("analysis_base")
            or "join_windows" not in descriptor or descriptor.get("homophones")):
        raise ValueError("A remeasured transcript needs the same signed base and admitted Name cuts.")
    raw_sha = (row.get("source_wave_sha256") if row.get("native_source")
               else row.get("analysis_base", {}).get("wave_sha256", row["stock_wav_sha256"]))
    unit = digest((raw_sha + "\n" + clock_transcript + "\n16/15").encode())
    if descriptor["unit"] != unit:
        raise ValueError("The measured paced analysis does not own this source recording.")
    prefix = f"paced-analysis/{unit}"
    phones_raw = members[f"{prefix}.phones.json"]
    if digest(phones_raw) != descriptor["phones_sha256"]:
        raise ValueError("The measured paced analysis differs from its checked plan.")
    phones = json.loads(phones_raw)
    identity = {"sha256": descriptor["wave_sha256"], "sample_rate": RATE, "sample_count": descriptor["samples"]}
    words = bind_paced_transcript(phones, identity, measured_transcript, transcript, row["base_phones"],
                                 descriptor.get("homophones", []))
    if "join_windows" in descriptor:
        return read_paced_join_windows(prefix, descriptor, row, words, members), words
    wave_raw = read_paced_wave(prefix, descriptor, members)
    if digest(wave_raw) != descriptor["wave_sha256"]:
        raise ValueError("The measured paced analysis differs from its checked plan.")
    try:
        with sf.SoundFile(BytesIO(wave_raw)) as source:
            if source.format not in {"WAV", "WAVEX"} or (source.subtype, source.samplerate, source.channels, source.frames) != (
                    "FLOAT", RATE, 1, descriptor["samples"]):
                raise ValueError("The measured paced waveform has the wrong sample clock.")
            samples = source.read(dtype="float32")
    except sf.LibsndfileError as error:
        raise ValueError("The measured paced waveform is unreadable.") from error
    if len(samples) != descriptor["samples"] or not np.isfinite(samples).all():
        raise ValueError("The measured paced waveform is invalid.")
    return samples, words


def caption_word_samples(row):
    words = row["base_phones"]["words"]
    transcript = expanded(row["stock_text"]).strip()
    samples = []
    for token in re.finditer(r"\S+", transcript):
        starts = [word["start_sample"] for word in words
                  if token.start() <= word["char_start"] < token.end()]
        if not starts:
            if re.search(r"\w", token.group()):
                raise ValueError("A reconstructed caption token has no measured word start.")
            # Standalone punctuation has no spoken phoneme. Anchor it at the
            # preceding measured release, or the first onset when it leads.
            prior = [word["end_sample"] for word in words if word["char_end"] <= token.start()]
            following = [word["start_sample"] for word in words if word["char_start"] >= token.end()]
            starts = prior[-1:] or following[:1]
            if not starts:
                raise ValueError("A reconstructed caption has no measured speech boundary.")
        samples.append(starts[0])
    return samples


def reconstructed_caption(row):
    if row.get('native_source') and row['native_route']['kind']=='quest':
        return {'text':expanded(row['stock_text']).strip(),'word_samples':caption_word_samples(row),
                'event':{'accept':1,'progress':2,'complete':3}[row['native_route']['event']],
                'quest_text':row['native_route']['original_text']}
    if analysis := row.get("analysis_base"):
        event = {"accept": 1, "progress": 2, "complete": 3}[row["source_id"].split(":")[2]]
        return {"text": expanded(row["stock_text"]).strip(), "word_samples": caption_word_samples(row),
                "event": event, "quest_text": analysis["quest_text"]}


def unpack_plans(data, *, progress=None, should_stop=None):
    import_progress(progress, should_stop, "opening")
    if result := cached_plan(data):
        return result
    result = unpack("plans", data, progress=progress, should_stop=should_stop)
    return remember_plan(data, result)


def unpack(kind, data, *, progress=None, should_stop=None, admitted_plan=False):
    expected, limit, rows = INPUTS[kind]
    members = safe_members(data, limit, max_members=40_000 if kind == "plans" else 500, lazy=kind == "plans")
    try:
        manifest = json.loads(members["input.json"])
        if manifest.get("format") != expected or not isinstance(manifest.get(rows), list):
            raise ValueError("This ZIP is not the selected type of Lite input.")
        if kind == "plans":
            if not admitted_plan:
                verified_rows(members, manifest, progress=progress, should_stop=should_stop)
        else:
            for item in manifest[rows]:
                audio = members[item["wav"]]
                if digest(audio) != item["wav_sha256"] or item["phones"]["audio_sha256"] != item["wav_sha256"]:
                    raise ValueError("A saved Fish recording differs from its receipt or phoneme clock.")
    except (KeyError, TypeError, UnicodeError, AttributeError) as error:
        raise ValueError("The Lite input is missing required plan or receipt fields.") from error
    return manifest, members


def reuse_plan_copy(target, expected, manifest, size, *, progress=None, should_stop=None):
    if target.is_symlink() or not target.is_file():
        return False
    try:
        identity = archive_identity(target)
        if identity[2] != size:
            return False
        if cached_plan(target, expected):
            return True
        if checked_hash(target, progress=progress, should_stop=should_stop,
                        stage="checking the installed catalog") != expected:
            return False
        installed = ArchiveMembers(target, INPUTS["plans"][1], 40_000)
        if installed.identity != identity:
            return False
    except (FileNotFoundError, BadZipFile):
        return False
    remember_plan(target, (manifest, installed), expected)
    return True


def import_input(app, payload, *, progress=None, should_stop=None, build_id=None, installed_addon=False):
    kind, path, expected = (payload.get(key) for key in ("kind", "path", "sha256"))
    if (kind not in INPUTS or not isinstance(path, str) or not isinstance(expected, str)
            or not HASH.fullmatch(expected)):
        raise ValueError("Choose the input ZIP and enter its published SHA-256.")
    source = Path(path).expanduser()
    if not source.is_absolute() or not source.is_file() or source.stat().st_size > INPUTS[kind][1]:
        raise ValueError("Choose a bounded local Lite input ZIP using its full path.")
    source_identity = archive_identity(source)
    if checked_hash(source, progress=progress, should_stop=should_stop) != expected:
        raise ValueError("The Lite input does not match the supplied SHA-256.")
    records = app.store.setting(SETTING, {})
    admission = {"build": build_id, "sha256": expected}
    previous = records.get("plans", {})
    if (kind == "plans" and build_id and previous.get("sha256") == expected
            and app.store.setting(PLAN_ADMISSION) == admission
            and previous.get("installed_path") == (str(source) if installed_addon else None)):
        try:
            imported(app, kind, progress=progress, should_stop=should_stop, build_id=build_id)
        except InputImportCancelled:
            raise
        except (OSError, ValueError, BadZipFile):
            pass  # Reverify the supplied input and restore the private copy.
        else:
            if archive_identity(source) != source_identity:
                raise ValueError("The prepared catalog changed during verification.")
            return previous
    manifest, members = (unpack_plans(source, progress=progress, should_stop=should_stop)
                         if kind == "plans" else unpack(kind, source.read_bytes()))
    if (kind == "plans" and (members.companion_joins or members.omitted_voice_media or members.name_fetch_holds)
            and not installed_addon):
        raise ValueError("Install this name data alongside the DMSpeaks shared playback add-on.")
    if archive_identity(source) != source_identity:
        raise ValueError("The prepared catalog changed during verification.")
    target = app.folder / "lite-inputs" / f"{kind}-{expected}.zip"
    if kind == "plans" and not installed_addon:
        target.parent.mkdir(parents=True, exist_ok=True)
        if reuse_plan_copy(target, expected, manifest, source_identity[2],
                           progress=progress, should_stop=should_stop):
            if archive_identity(source) != source_identity:
                raise ValueError("The prepared catalog changed during import.")
        else:
            save_plan_copy(source, target, expected, manifest, source_identity,
                           progress=progress, should_stop=should_stop)
    entry = dict(sha256=expected, count=len(manifest[INPUTS[kind][2]]), name=source.name)
    stored = {**entry, "path": str(source)} if kind == "returns" else entry
    if installed_addon:
        stored = {**entry, "installed_path": str(source)}
        remember_plan(source, (manifest, members), expected)
    records = app.store.setting(SETTING, {})
    app.store.set_setting(SETTING, {**records, kind:stored})
    if kind == "plans" and build_id:
        app.store.set_setting(PLAN_ADMISSION, admission)
    return entry


def save_plan_copy(source, target, expected, manifest, identity, *, progress=None, should_stop=None):
    stage = target.with_name(f".{target.name}.partial")
    with hold(target.parent, ".catalog-write.lock"):
        # Only this archive's abandoned staging file can be removed after a crash.
        stage.unlink(missing_ok=True)
        try:
            copied, checksum = 0, sha256()
            with source.open("rb") as original, stage.open("xb") as stream:
                while block := original.read(1024 * 1024):
                    import_progress(progress, should_stop, "saving", copied, identity[2])
                    checksum.update(block)
                    stream.write(block)
                    copied += len(block)
            if checksum.hexdigest() != expected or archive_identity(source) != identity:
                raise ValueError("The prepared catalog changed during import.")
            import_progress(progress, should_stop, "saving", copied, identity[2])
            stage.replace(target)
            # Bind lazy media reads to the verified private copy.
            relocated = ArchiveMembers(target, INPUTS["plans"][1], 40_000)
            remember_plan(target, (manifest, relocated), expected)
        finally:
            stage.unlink(missing_ok=True)


def imported(app, kind, required=True, *, progress=None, should_stop=None, build_id=None):
    item = app.store.setting(SETTING, {}).get(kind)
    if not item:
        if required:
            raise ValueError("Import and verify the prepared dialogue plans in WoW Lite first.")
        return {}, {}
    if kind == "returns":
        path = Path(item["path"])
    elif "installed_path" in item:
        path = Path(item["installed_path"])
        if path.resolve() != path:
            raise ValueError("Reinstall the DMSpeaks add-on in CurseForge to repair its Name catalog.")
    else:
        path = app.folder / "lite-inputs" / f"{kind}-{item['sha256']}.zip"
    if path.stat().st_size > INPUTS[kind][1]:
        raise ValueError("The imported Lite input exceeds its original size limit.")
    import_progress(progress, should_stop, "opening")
    if kind == "plans" and (result := cached_plan(path, item["sha256"])):
        return result
    identity = archive_identity(path)
    checksum = (checked_hash(path, progress=progress, should_stop=should_stop)
                if progress or should_stop else file_hash(path))
    if checksum != item["sha256"]:
        raise ValueError("The imported Lite input changed. Import it again from the verified ZIP.")
    admission = {"build": build_id, "sha256": checksum}
    if kind == "plans" and build_id and app.store.setting(PLAN_ADMISSION) == admission:
        # Exact bytes were admitted by this same companion build. Reopen the
        # bounded archive without decoding all its reference audio again.
        result = unpack(kind, path, admitted_plan=True)
        if len(result[0]["dialogues"]) != item["count"]:
            raise ValueError("The imported catalog receipt has an incorrect dialogue count.")
    else:
        result = (unpack_plans(path, progress=progress, should_stop=should_stop)
                  if kind == "plans" else unpack(kind, path.read_bytes()))
    if archive_identity(path) != identity:
        raise ValueError("The imported Lite input changed during verification.")
    if kind == "plans":
        if build_id and app.store.setting(PLAN_ADMISSION) != admission:
            app.store.set_setting(PLAN_ADMISSION, admission)
        return remember_plan(path, result, item["sha256"])
    return result


@lru_cache(maxsize=8)
def signed_text_assignments(content):
    """Index one exact, signed version of the installed pack transcript."""
    assignments = {}
    for raw in content.decode("utf-8").splitlines():
        line = raw.strip()
        if line.startswith('["'):
            end = line.find('"] = ', 2)
            if end != -1:
                assignments.setdefault(line[2:end], line[end + 5:].rstrip(","))
    return assignments


def signed_stock_transcript(row):
    analysis = row.get("analysis_base")
    return row.get("signed_stock_caption", analysis["reference"]["transcript"] if analysis else row["stock_text"])


def recorded_stock_transcript(row, members=None):
    """Use the measured words while separately verifying the installed signed caption."""
    signed = signed_stock_transcript(row)
    # A signed text table may spell out a different class from the one in the
    # actual performance. Use the reviewed recording transcript only for the
    # exact plan, original waveform, and signed text named by the catalog.
    if proofs := getattr(members, "verified_name_playback", None):
        from lite_portable_references import canonical

        proof = proofs.get(digest(canonical(row)), {})
        if binding := proof.get("complete_stock_transcript"):
            analysis = row.get("analysis_base")
            recorded = analysis["reference"]["transcript"] if analysis else row["stock_text"]
            if binding != {"ogg_sha256": row["stock_sha256"], "wav_sha256": row["stock_wav_sha256"],
                           "signed_text_sha256": digest(signed.encode()), "text_sha256": digest(recorded.encode())}:
                raise ValueError("The reviewed transcript belongs to a different original recording.")
            return recorded
    # Quinn's signed caption skips the narrator's second sentence. The complete
    # recording and its two independently checked texts identify this performance.
    if (row.get("source_id"), row.get("npc_id"), row.get("stock_sha256")) == (
            "q:91921:complete", 1951, "e1b43c9cccf0a621161754de6f4aa4a75819477af5de7d0ad7299f137ee9c880"):
        recorded = row["stock_text"]
        if (row.get("stock_wav_sha256") != "376aeedec942077cd75ec4818e812e8a720310bde0805090cc494950cbf819da"
                or digest(signed.encode()) != "6217475256bc5b8ce9980cf54f3e7ece2998523d1579d22c69a83fc5862ce29c"
                or digest(recorded.encode()) != "7a1d99d390140012c6f97c3f9e8c41d552e1078881b20521cfc5d5f26c03d444"):
            raise ValueError("The measured Quinn Yorick narration differs from the verified complete recording.")
        return recorded
    proof = row.get("recorded_stock_opening")
    if proof is None:
        return signed
    if (not isinstance(proof, dict) or set(proof) != {"omitted_caption", "original_ogg_sha256",
            "original_wave_sha256", "full_caption_sha256", "trimmed_caption_sha256"}
            or not row.get("signed_stock_caption") or not row.get("requires_paced_base") or row.get("analysis_base")
            or proof["original_ogg_sha256"] != row["stock_sha256"]
            or proof["original_wave_sha256"] != row["stock_wav_sha256"]
            or proof["full_caption_sha256"] != digest(signed.encode())):
        raise ValueError("The measured spoken opening differs from its signed recording.")
    omitted = proof["omitted_caption"]
    if not isinstance(omitted, str) or not omitted or not signed.startswith(omitted):
        raise ValueError("The recorded opening differs from the signed caption.")
    recorded = signed[len(omitted):].lstrip()
    if (not recorded or recorded != row["stock_text"]
            or proof["trimmed_caption_sha256"] != digest(expanded(recorded).strip().encode())):
        raise ValueError("The recorded opening differs from the independently measured words.")
    return recorded


class MissingStockRecording(ValueError):
    """The catalogued Ogg is absent from this install."""


def installed(addons, row):
    if row.get('native_source'):
        return None
    root = addons.resolve()
    source = root / row["stock_path"]
    path = source.resolve()
    if not path.is_relative_to(root):
        raise ValueError("Install the matching signed Spoken recording in the selected AddOns folder.")
    if path == source:
        try:
            path.stat()
        except FileNotFoundError as error:
            raise MissingStockRecording("Install the matching signed Spoken recording in the selected AddOns folder.") from error
    if not path.is_file() or file_hash(path) != row["stock_sha256"]:
        raise ValueError("Install the matching signed Spoken recording in the selected AddOns folder.")
    pack = row["stock_path"].split("/",1)[0]
    if row["stock_path"] == _DEFIAS_FOLLOWUP_PUBLISHER:
        for name, checksum in _DEFIAS_FOLLOWUP_LOOKUPS.items():
            lookup = (root / pack / "generated" / name).resolve()
            if not lookup.is_relative_to(root) or not lookup.is_file() or file_hash(lookup) != checksum:
                raise ValueError("Install the signed Spoken follow-up lookups for this recording.")
    table = (root / pack / "generated/text_table.lua").resolve()
    if not table.is_relative_to(root) or not table.is_file():
        raise ValueError("Install the matching signed Spoken recording and text table.")
    with table.open("rb") as stream:
        content = stream.read(MAX_TABLE_BYTES + 1)
    if len(content) > MAX_TABLE_BYTES:
        raise ValueError("Install the matching signed Spoken recording and text table.")
    signed_transcript = signed_stock_transcript(row)
    if digest(content) != row["text_table_sha256"]:
        if (PurePosixPath(row["stock_path"]).stem != row["key"] or not matches_edited_table(
                content, pack, row["text_table_sha256"], row["key"], signed_transcript)):
            raise ValueError("Install the matching signed Spoken recording and text table.")
        return path
    assignment = signed_text_assignments(content).get(row["key"])
    # Spoken's Lua exporter uses decimal escapes for control bytes such as tab.
    if assignment is not None:
        assignment = re.sub(r"(?<!\\)((?:\\\\)*)\\([0-9]{3})",
                            lambda match: match[1] + f"\\u{int(match[2]):04x}", assignment)
    if assignment is None or json.loads(assignment) != signed_transcript:
        raise ValueError("The planned transcript differs from the signed stock text.")
    return path


def installed_plans(addons, manifest, *, gender=""):
    """Choose an exact installed recording before counting coverage or requesting speech."""
    rows = manifest["dialogues"]
    variants = manifest.get("stock_revisions")
    if not variants:
        return rows
    selected = []
    for row in rows:
        key = source_key(row)
        for variant in variants.get(key, ()):
            alternate = variant["row"]
            renamed = alternate["stock_path"] != row["stock_path"]
            if renamed and alternate.get("sexes") and gender not in alternate["sexes"]:
                continue
            try:
                installed(addons, alternate)
            except (OSError, ValueError):
                continue
            row = {**alternate, "catalog_source_key": key} if renamed else alternate
            break
        selected.append(row)
    return selected


def stock_decode_contract(row):
    """Bind the decoder to the signed recording, including excerpt parents."""
    if "signed_reference_parent" in row:
        return {**row["signed_reference_parent"], "stock_sha256": row["stock_sha256"]}
    if "analysis_base" not in row:
        return row
    reference = row["analysis_base"]["reference"]
    if "stock_pcm_repair" not in row:
        return {**reference, "stock_sha256": row["stock_sha256"]}
    return {**reference, "stock_sha256": row["stock_sha256"],
            "stock_pcm_repair": row["stock_pcm_repair"]}


def verify_signed_reference_parent(row):
    """Bind a complete signed decode and its matching prefix transcript."""
    parent = row["signed_reference_parent"]
    reference = (row.get("analysis_base") or {}).get("reference")
    caption = row.get("signed_stock_caption")
    if (not isinstance(parent, dict) or set(parent) != {"samples", "pcm_sha256"}
            or type(parent["samples"]) is not int or not 0 < parent["samples"] <= RATE * 120
            or not isinstance(parent["pcm_sha256"], str) or not HASH.fullmatch(parent["pcm_sha256"])
            or row.get("reference_from_stock") is not True or "stock_pcm_repair" in row
            or not isinstance(reference, dict)
            or type(reference.get("samples")) is not int or not 0 < reference["samples"] < parent["samples"]
            or not isinstance(caption, str) or not isinstance(reference.get("transcript"), str)):
        raise ValueError("A signed reference excerpt needs its complete installed recording and measured range.")
    prefix = reference["transcript"]
    if (not prefix or not caption.startswith(prefix) or len(prefix) >= len(caption)
            or not caption[len(prefix)].isspace() or prefix[-1] not in ".!?"):
        raise ValueError("The signed reference excerpt does not end at a complete sentence in the checked caption.")


def verify_stock_reference_slice(row):
    """Bind the provider excerpt separately from the full signed playback clock."""
    from reference_media import ENVELOPE_FIELDS

    binding = row["stock_reference_slice"]
    if (not isinstance(binding, dict) or set(binding) != {"base_wave", "speech"}
            or row.get("reference_from_stock") is not True or row.get("requires_paced_base") is not True
            or any(field in row for field in ("analysis_base", "signed_reference_parent", "signed_stock_caption"))):
        raise ValueError("The signed playback excerpt needs a full paired stock clock.")
    base, speech = binding["base_wave"], binding["speech"]
    if (not isinstance(base, dict) or base.keys() != ENVELOPE_FIELDS
            or (base["wav_sha256"], base["pcm_sha256"], base["samples"], base["sample_rate"]) != (
                row["stock_wav_sha256"], row["pcm_sha256"], row["samples"], RATE)
            or any(not isinstance(base[field], str) for field in ("wave_prefix", "wave_suffix"))
            or len(base["wave_prefix"]) + len(base["wave_suffix"]) > 90000
            or not isinstance(speech, dict) or set(speech) != {"wav_sha256", "pcm_sha256", "samples", "transcript"}
            or any(not isinstance(speech[field], str) or not HASH.fullmatch(speech[field])
                   for field in ("wav_sha256", "pcm_sha256"))
            or type(speech["samples"]) is not int or not 0 < speech["samples"] < row["samples"]):
        raise ValueError("The signed playback excerpt differs from the prepared sample clock.")
    prefix, caption = speech["transcript"], row["stock_text"]
    if (not isinstance(prefix, str) or not prefix or not caption.startswith(prefix) or len(prefix) >= len(caption)
            or not caption[len(prefix)].isspace() or prefix[-1] not in ".!?"):
        raise ValueError("The signed playback excerpt needs a complete sentence from the checked caption.")


def verify_stock_pcm_repair(row):
    repair = row["stock_pcm_repair"]
    if (not isinstance(row.get("stock_sha256"), str) or not HASH.fullmatch(row["stock_sha256"])
            or not isinstance(repair, dict) or repair.keys() != {"scalar_pcm_sha256", "adjustments"}
            or not isinstance(repair["scalar_pcm_sha256"], str) or not HASH.fullmatch(repair["scalar_pcm_sha256"])
            or type(row.get("samples")) is not int or not 0 < row["samples"] <= RATE * 120
            or not isinstance(repair["adjustments"], list)):
        raise ValueError("The stock decoder correction needs its exact signed sample contract.")
    previous = -1
    for pair in repair["adjustments"]:
        if (not isinstance(pair, list) or len(pair) != 2 or type(pair[0]) is not int
                or not previous < pair[0] < row["samples"] or type(pair[1]) is not int or pair[1] not in (-1,1)):
            raise ValueError("The stock decoder correction contains an invalid sample adjustment.")
        previous = pair[0]


def read_stock_pcm(path, staged, decoder_options=()):
    decoder = "ffmpeg"
    process_context = {}
    bundled = sys.platform == "win32" and PACKAGED_RUNTIME.is_dir()
    if bundled:
        # A bare executable name can select a decoder in the Windows working directory.
        executable = PACKAGED_RUNTIME / "bin/ffmpeg.exe"
        if not executable.is_file():
            raise ValueError("The companion's bundled audio decoder is missing. Repair DMSpeaks Lite and retry.")
        decoder = str(executable)
        # Keep the shared codec DLLs on the same build as the executable.
        process_context = {"cwd": executable.parent, "env": {**os.environ, "PATH": str(executable.parent)}}
        path, staged = Path(path).absolute(), Path(staged).absolute()
    try:
        subprocess.run([decoder, "-nostdin", "-v", "error", "-y", *decoder_options, "-i", str(path),
             "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(staged)],
            check=True, capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            **process_context)
    except (OSError, subprocess.CalledProcessError) as error:
        # CreateProcess can report a loader error, or the child can exit with its NTSTATUS.
        # Limit the repair instruction to missing/unloadable executable files and DLLs.
        load_error = getattr(error, "winerror", None) in {2, 3, 126, 127, 193, 216, 1114}
        child_status = getattr(error, "returncode", 0) & 0xffffffff
        if bundled and (load_error or child_status in {0xc000007b, 0xc0000135, 0xc0000139, 0xc0000142}):
            raise ValueError("Windows could not load the bundled audio decoder. Repair DMSpeaks Lite and retry.") from error
        raise
    return sf.read(staged, dtype="int16")


@lru_cache(maxsize=1)
def bundled_stock_pcm_repairs():
    # Published plans can predate the Windows rounding table for their Ogg.
    path = Path(__file__).with_name("formats") / "wow-stock-pcm-repairs.json.gz"
    message = "The installed audio decoder data is missing or damaged. Run DMSpeaks Lite Setup to repair it."
    try:
        archive = path.read_bytes()
    except OSError as error:
        raise ValueError(message) from error
    if digest(archive) != STOCK_PCM_REPAIRS_SHA256:
        raise ValueError(message)
    return json.loads(gzip.decompress(archive))


def recover_stock_pcm(path, staged, row):
    stock_hash = file_hash(path)
    if stock_hash != row["stock_sha256"]:
        raise ValueError("Install the matching signed Spoken recording in the selected AddOns folder.")
    pcm, rate = read_stock_pcm(path, staged, ("-cpuflags", "0", "-flags", "+bitexact"))
    if rate != RATE or pcm.ndim != 1 or len(pcm) != row["samples"]:
        raise ValueError("The original Spoken audio decoded with a different sample count or rate.")
    scalar_hash = digest(pcm.astype("<i2", copy=False).tobytes())
    if scalar_hash == row["pcm_sha256"]:
        return pcm
    repair = row.get("stock_pcm_repair")
    if repair is None:
        supplemental = bundled_stock_pcm_repairs().get(stock_hash)
        if supplemental is None or (supplemental["samples"], supplemental["pcm_sha256"]) != (
                row["samples"], row["pcm_sha256"]):
            raise ValueError("Lite could not reproduce the original Spoken audio for this dialogue. "
                             "The companion needs updated decoder data.")
        repair = {key: supplemental[key] for key in ("scalar_pcm_sha256", "adjustments")}
    verify_stock_pcm_repair({**row, "stock_pcm_repair": repair})
    if scalar_hash != repair["scalar_pcm_sha256"]:
        raise ValueError("This stock decoder did not reproduce the checked recovery PCM.")
    for frame, delta in repair["adjustments"]:
        value = int(pcm[frame]) + delta
        if not -32768 <= value <= 32767:
            raise ValueError("The stock decoder correction exceeds the signed PCM range.")
        pcm[frame] = value
    return sample_checked(pcm, rate, row)


def decode_stock(path, row, output):
    with TemporaryDirectory(dir=output.parent, prefix=".decode-") as folder:
        staged = Path(folder) / "stock.wav"
        pcm, rate = read_stock_pcm(path, staged)
        try:
            sample_checked(pcm, rate, row)
        except ValueError:
            if not row.get("stock_sha256"):
                raise
            pcm = recover_stock_pcm(path, staged, row)
    cache_bytes(output, wave_bytes(pcm))
    return pcm.astype("float32") / 32768


def wave_bytes(pcm):
    # Rewrap the sample-verified audio with a stable PCM header for cache identity.
    out = BytesIO()
    with wave.open(out, "wb") as file:
        file.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        file.writeframes(pcm.astype("<i2", copy=False).tobytes())
    return out.getvalue()


def sample_checked(pcm, rate, row):
    if (rate != RATE or pcm.ndim != 1 or len(pcm) != row["samples"]
            or digest(pcm.astype("<i2", copy=False).tobytes()) != row["pcm_sha256"]):
        raise ValueError("The checked recording decoded to a different sample clock or PCM checksum.")
    return pcm


def exact_paced_stock_wave(row, references):
    """Reuse the complete reference WAV pinned to the prepared source phones."""
    from lite_portable_references import wave_data

    dialogue = references[-1]
    if (dialogue["id"] != row["source_id"] + ":dialogue"
            or dialogue["sha256"] != row["stock_wav_sha256"]):
        raise ValueError("The ordered dialogue reference is not the complete measured stock WAV.")
    audio = dialogue["wav_bytes"]
    _, _, samples = wave_data(audio)
    if len(samples) != 2 * row["samples"] or digest(samples) != row["pcm_sha256"]:
        raise ValueError("The measured stock WAV differs from the signed dialogue PCM.")
    return audio


def render_inputs(stock, row, reference, members, *, signed_reference=None):
    if row.get('native_source'):
        from lite_native_sources import base_pcm
        return base_pcm(row,members,reference)
    if "analysis_base" not in row:
        pcm = decode_stock(stock, row, reference)
        if row.get("requires_paced_base"):
            from lite_portable_references import references_for_stock

            references = references_for_stock(row, members, reference)
            if binding := row.get("stock_reference_slice"):
                from reference_media import restore_wave, wave_data

                cache_bytes(reference, restore_wave(binding["base_wave"], wave_data(reference.read_bytes())[2]))
            else:
                cache_bytes(reference, exact_paced_stock_wave(row, references))
        return pcm
    if stock is not None:
        decode_stock(stock, stock_decode_contract(row), signed_reference or reference)
    pcm = analysis_pcm(row, members)
    if row.get("requires_paced_base") or stock is None:
        cache_bytes(reference, wave_bytes(pcm))
    return pcm.astype("float32") / 32768
