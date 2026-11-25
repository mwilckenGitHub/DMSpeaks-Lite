"""Verify portable ordered NPC references without using the playback-base clock."""

from base64 import b64decode
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re

from lite_portable_io import evidence_matches
from reference_media import (ENVELOPE_FIELDS, FIELDS, FORMAT as MEDIA_FORMAT, MAX_SAMPLES, RATE,
                             restore_wave, unpack_wave, wave_data)


FORMAT = "dmspeaks-portable-references-1"
POLICY = "dmspeaks-ordered-reference-v1"
HASH = re.compile(r"[0-9a-f]{64}")
PREFIX_KINDS = {"game_prefix", "known_spoken_voice_pack", "casting_candidate"}
FISH_ACCENT_PREFIX = re.compile(r"\[[A-Za-z][A-Za-z -]{0,48} accent\] ")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(data):
    return sha256(data).hexdigest()


def locale_key(locale):
    return re.sub(r"[-_]", "", locale).casefold()


def contract_member(checksum):
    if not isinstance(checksum, str) or not HASH.fullmatch(checksum):
        raise ValueError("The prepared dialogue needs a complete reference contract identity.")
    return f"references/contracts/{checksum}.json"


def deferred_reference(row, members):
    find = getattr(members, "installed_reference", None)
    if find is not None and row.get("reference_contract"):
        return find(contract_member(row["reference_contract"]["sha256"]))


def signed_source_needed(row, members):
    return row.get("reference_from_stock") is True or deferred_reference(row, members) is not None


def checked_media_envelope(media):
    if (media.keys() != FIELDS or media["format"] != MEDIA_FORMAT or media["sample_rate"] != RATE
            or type(media["samples"]) is not int or not 0 < media["samples"] <= MAX_SAMPLES
            or any(not isinstance(media[key], str) or not HASH.fullmatch(media[key])
                   for key in ("wav_sha256", "pcm_sha256", "flac_sha256"))
            or any(not isinstance(media[key], str) for key in ("wave_prefix", "wave_suffix"))
            or len(media["wave_prefix"]) + len(media["wave_suffix"]) > 90000):
        raise ValueError("The checked reference media has an invalid sample clock or WAV envelope.")
    try:
        length = sum(len(b64decode(media[key], validate=True)) for key in ("wave_prefix", "wave_suffix"))
    except ValueError as error:
        raise ValueError("The checked reference media has invalid WAV metadata.") from error
    if length > 65536:
        raise ValueError("The checked reference media has excessive WAV metadata.")
    return {key: media[key] for key in ENVELOPE_FIELDS}


def decode_references(row, members, *, stock_pcm=None, defer_stock=False):
    """Return exact ordered WAV/transcript pairs and their admitted contract."""
    binding = row.get("reference_contract")
    if not isinstance(binding, dict) or set(binding) != {"sha256"}:
        raise ValueError("The prepared dialogue needs its ordered reference contract.")
    checksum = binding["sha256"]
    member = contract_member(checksum)
    raw = members[member]
    if len(raw) > 2_000_000 or not evidence_matches(members, member, raw, checksum):
        raise ValueError("The prepared reference contract changed.")
    contract = json.loads(raw)
    fields = {"format", "mode", "source", "upstream", "parts", "assembled"}
    if (not isinstance(contract, dict) or set(contract) - {"fish_accent_prefix"} != fields
            or contract["format"] != FORMAT
            or contract["mode"] not in {"original", "stock-bound", "analysis-bound"}):
        raise ValueError("The prepared references have an unsupported source contract.")
    prefix = contract.get("fish_accent_prefix")
    if (("fish_accent_prefix" in contract) != ("fish_accent_prefix" in row)
            or ("fish_accent_prefix" in contract and (
                not isinstance(prefix, str) or not FISH_ACCENT_PREFIX.fullmatch(prefix)
                or prefix != row["fish_accent_prefix"]))):
        raise ValueError("The prepared Fish accent differs from its ordered reference contract.")
    source = contract["source"]
    if (not isinstance(source, dict) or set(source) != {"source_id", "npc_id", "locale", "source_text_sha256"}
            or (source["source_id"], source["npc_id"], source["source_text_sha256"]) !=
               (row["source_id"], row["npc_id"], row["source_text_sha256"])
            or locale_key(source["locale"]) != locale_key(row["locale"])):
        raise ValueError("The ordered references belong to another dialogue, NPC or locale.")
    upstream = contract["upstream"]
    if (not isinstance(upstream, dict) or set(upstream) != {"contract", "sha256"}
            or digest(canonical(upstream["contract"])) != upstream["sha256"]):
        raise ValueError("The independent reference-provenance contract changed.")
    proof = upstream["contract"]
    if (proof.get("policy") != POLICY or proof.get("sample_rate") != RATE
            or (proof.get("destination_source_id"), proof.get("destination_npc_id"),
                proof.get("destination_source_text_sha256")) !=
               (source["source_id"], source["npc_id"], source["source_text_sha256"])
            or locale_key(proof["locale"]) != locale_key(source["locale"])):
        raise ValueError("The independent reference proof has a different policy or source owner.")
    declarations = proof.get("ordered_parts")
    parts = contract["parts"]
    if (not isinstance(declarations, list) or not 1 <= len(declarations) <= 2
            or not isinstance(parts, list) or not 1 <= len(parts) <= 2
            or digest("\n\n".join(part["transcript"] for part in declarations).encode()) != proof["transcript_sha256"]):
        raise ValueError("The ordered reference proof lacks its exact source transcripts.")
    if contract["mode"] == "original":
        if len(parts) != len(declarations) or contract["assembled"]["wav_sha256"] != proof["reference_audio_sha256"]:
            raise ValueError("The original reference assembly differs from its independent proof.")
    else:
        if len(parts) != 2 or declarations[0].get("kind") not in PREFIX_KINDS:
            raise ValueError("The refreshed reference needs its admitted NPC voice before the signed dialogue.")

    deferred = deferred_reference(row, members)
    if deferred is not None:
        if (contract["mode"] != "original" or row.get("reference_from_stock")
                or not row.get("requires_paced_base") or row.get("native_source")
                or row.get("expanded_name_binding") or not row.get("stock_path")
                or parts[-1]["member"] != deferred or declarations[-1]["kind"] != "signed_spoken_dialogue"
                or declarations[-1].get("source_audio_sha256") != row["stock_sha256"]
                or declarations[-1].get("decoded_audio_sha256") != row["stock_wav_sha256"]
                or declarations[-1].get("source_id", row["source_id"]) != row["source_id"]
                or declarations[-1].get("text_table_sha256", row["text_table_sha256"]) != row["text_table_sha256"]):
            raise ValueError("The omitted dialogue must match the complete installed signed recording.")

    if "game_object_reference" in row:
        paired_stock = deferred is not None and row.get("paced_base_analysis")
        if (row["game_object_reference"] is not True or not (row.get("analysis_base") or paired_stock)
                or row.get("requires_paced_base") is not True or row.get("reference_from_stock")
                or not re.fullmatch(r"q:[0-9]+:(accept|progress|complete)", row["source_id"])
                or contract["mode"] != "original" or len(declarations) != 1
                or declarations[0].get("kind") != "signed_spoken_dialogue"
                or declarations[0].get("source_id") != row["source_id"]
                or declarations[0].get("source_audio_sha256") != row["stock_sha256"]
                or declarations[0].get("decoded_audio_sha256") != row["stock_wav_sha256"]
                or parts[0]["media"]["wav_sha256"] != row["stock_wav_sha256"]):
            raise ValueError("A game object reconstruction needs its complete matching signed dialogue.")

    references = []
    all_pcm = []
    frames = 0
    missing_audio = False
    use_stock = row.get("reference_from_stock") is True
    analysis = row.get("analysis_base")
    analysis_bound = contract["mode"] == "analysis-bound"
    if analysis_bound and (row.get("requires_paced_base") is not True or not analysis or use_stock):
        raise ValueError("The generated dialogue reference needs its exact paired analysis base.")
    if use_stock and ("companion_seed" not in row or not row.get("stock_path") or len(parts) != 2
                      or "native_source" in row or "expanded_name_binding" in row):
        raise ValueError("Installed reference audio needs a paired signed Name base and ordered voice prefix.")
    sliced = row.get("stock_reference_slice")
    signed = sliced["speech"] if sliced else analysis["reference"] if analysis else row
    signed_wav = signed["wav_sha256"] if sliced else row.get("stock_wav_sha256")
    if sliced and (not use_stock or contract["mode"] != "stock-bound"):
        raise ValueError("The signed playback excerpt needs the installed reference contract.")
    omitted_voice_media = getattr(members, "omitted_voice_media", ())
    for index, part in enumerate(parts):
        if (not isinstance(part, dict) or set(part) != {"id", "member", "media", "transcript", "origin"}
                or part["id"] != f"{row['source_id']}:{'voice' if index == 0 and len(parts) == 2 else 'dialogue'}"
                or not isinstance(part["transcript"], str) or not 0 < len(part["transcript"]) <= 10000
                or part["member"] != f"references/media/{part['media']['flac_sha256']}.flac"):
            raise ValueError("The prepared reference has an invalid ordered audio/transcript pair.")
        media = part["media"]
        from_stock = (use_stock and index == 1) or (
            deferred is not None and index == len(parts) - 1)
        if from_stock:
            envelope = checked_media_envelope(media)
            if (media["wav_sha256"], media["pcm_sha256"], media["samples"]) != (
                    signed_wav, signed["pcm_sha256"], signed["samples"]):
                raise ValueError("The installed dialogue reference does not match the signed stock sample clock.")
            if stock_pcm is not None:
                pcm = stock_pcm
                parent = sliced["base_wave"] if sliced else row.get("signed_reference_parent")
                if parent:
                    if (len(pcm) != parent["samples"] * 2 or digest(pcm) != parent["pcm_sha256"]):
                        raise ValueError("The complete installed signed PCM differs from the excerpt's measured source.")
                    pcm = pcm[:media["samples"] * 2]
                audio = restore_wave(envelope, pcm)
            elif defer_stock:
                audio, pcm = None, None
                missing_audio = True
            else:
                raise ValueError("Install the matching signed Spoken recording to reconstruct this reference.")
        elif part["member"] in omitted_voice_media and not row.get("native_source"):
            if (index != 0 or len(parts) != 2 or declarations[0]["kind"] not in PREFIX_KINDS
                    or not row.get("stock_path") or row.get("expanded_name_binding")):
                raise ValueError("A removed voice prefix cannot supply the original Spoken dialogue.")
            checked_media_envelope(media)
            audio, pcm = None, None
            missing_audio = True
        else:
            read_media = getattr(members, "reference_media", members.__getitem__)
            audio = unpack_wave(media, read_media(part["member"]))
            _, _, pcm = wave_data(audio)
        frames += media["samples"]
        if frames > MAX_SAMPLES:
            raise ValueError("The ordered references exceed the shared 120-second budget.")
        origin = part["origin"]
        expected_upstream = contract["mode"] == "original" or index == 0
        if expected_upstream:
            if origin != {"upstream_part": index}:
                raise ValueError("The reference source order differs from its admitted declaration.")
            declared = declarations[index]
            start, end = declared["source_samples"]
            if (declared.get("sample_rate") != RATE or start != 0 or end - start != part["media"]["samples"]
                    or declared["transcript"] != part["transcript"]):
                raise ValueError("The reference audio or transcript differs from its measured source interval.")
            if declared["kind"] == "signed_spoken_dialogue":
                if "word_range" not in declared and declared["decoded_audio_sha256"] != part["media"]["wav_sha256"]:
                    raise ValueError("The complete dialogue reference differs from its original WAV.")
                if "word_range" in declared and (not HASH.fullmatch(declared.get("clock_sha256", ""))
                                                  or declared["word_range"][0] != 0):
                    raise ValueError("The reference excerpt lacks its measured source clock.")
            elif declared["kind"] not in PREFIX_KINDS or declared["sha256"] != part["media"]["wav_sha256"]:
                raise ValueError("The NPC reference differs from its admitted original recording.")
        elif analysis_bound:
            if (origin != {"analysis_wav_sha256": analysis["wave_sha256"]}
                    or (media["wav_sha256"], media["pcm_sha256"], media["samples"], part["transcript"])
                    != (analysis["wave_sha256"], row["pcm_sha256"], row["samples"], row["stock_text"])):
                raise ValueError("The dialogue reference differs from the measured generated base.")
        else:
            expected = {"stock_path": row["stock_path"], "stock_sha256": row["stock_sha256"],
                        "text_table_sha256": row["text_table_sha256"]}
            reference = sliced["speech"] if sliced else row.get("analysis_base", {}).get("reference")
            captions = ((reference["transcript"],) if reference else
                        (row["stock_text"], row.get("signed_stock_caption")))
            if (origin != expected
                    or part["media"]["pcm_sha256"] != (reference["pcm_sha256"] if reference else row["pcm_sha256"])
                    or part["media"]["samples"] != (reference["samples"] if reference else row["samples"])
                    or part["transcript"] not in captions):
                raise ValueError("The refreshed voice reference differs from the exact signed dialogue clock.")
        references.append({"id": part["id"], "wav_bytes": audio, "transcript": part["transcript"],
                           "sha256": media["wav_sha256"]})
        if pcm is not None:
            all_pcm.append(pcm)
    if missing_audio:
        assembly = contract["assembled"]
        if (assembly.keys() != ENVELOPE_FIELDS or assembly["sample_rate"] != RATE
                or type(assembly["samples"]) is not int or assembly["samples"] != frames
                or any(not isinstance(assembly[key], str) or not HASH.fullmatch(assembly[key])
                       for key in ("wav_sha256", "pcm_sha256"))):
            raise ValueError("The deferred reference assembly has the wrong sample contract.")
    else:
        restore_wave(contract["assembled"], b"".join(all_pcm))
    return references, contract


def reference_evidence(references):
    return [{"id": part["id"], "audio_sha256": digest(part["wav_bytes"]),
             "transcript_sha256": digest(part["transcript"].encode())} for part in references]


def verified_references(row, members, *, stock_pcm=None, defer_stock=False):
    """Validate a descriptor while keeping malformed input errors user-readable."""
    try:
        return decode_references(row, members, stock_pcm=stock_pcm, defer_stock=defer_stock)
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError("The prepared ordered references are missing required source or media evidence.") from error


def verify_catalog_references(row, members):
    """Verify the bundled audio and declaration before the game path is available."""
    references, _ = verified_references(row, members, defer_stock=True)
    declared = [{"id": part["id"], "audio_sha256": part["sha256"],
                 "transcript_sha256": digest(part["transcript"].encode())} for part in references]
    if row.get("request_reference_evidence") != declared:
        raise ValueError("The prepared request references differ from their ordered audio/transcript contract.")
    return references


def references_for_stock(row, members, decoded_stock):
    """Recover the exact reference WAVs from the already validated stock decode."""
    pcm = None
    if signed_source_needed(row, members):
        _, _, pcm = wave_data(decoded_stock.read_bytes())
    return verified_references(row, members, stock_pcm=pcm)[0]


LEGACY_MISSING_SPOKEN = "This dialogue needs its original Spoken recording before Fish Audio can fetch its name."

# Each pinned plan has one name slot. Its complete fetch phrase occurs verbatim
# at a unique place in both texts. The reviewed edits fall outside that phrase.
# Pin the entire plan to bind the original audio, both texts, cut and playback seed.
VERIFIED_NAME_CONTEXTS = {
    "q:93797:complete": "76c456d798fcad85ec6738a5a383a47b4b8e96ede6486d235e5904c792cc41cd",
    "q:92701:accept": "ca4beb2735d39db6966c8ae57d98f04a87cea6758e4cc5c599c8a131cc685650",
    "q:99260:complete": "4c5a689f9c45910c5ae756a69889b167b63fbe6989aeadc3396486b660370d6b",
}


class OriginalSpokenNotCatalogued(ValueError):
    """Neither an original nor a usable default voice reference is available."""

    def __init__(self, row):
        source = row.get("source_id", "")
        quest = re.fullmatch(r"q:(\d+):([^:]+)", source)
        label = f"quest {quest[1]}" if quest else source
        npc = row.get("npc") or "this dialogue"
        subject = f"{npc} ({label})" if label else npc
        super().__init__(f"The installed name catalog has neither an original Spoken recording "
                         f"nor a usable default voice reference for {subject}.")


def require_hosted_playback_base(row, *, stock_missing=False, members=None, playback=None):
    """Admit an exact stock performance or a native playback plan with its default voice."""
    from lite_portable_inputs import recorded_stock_transcript, stock_decode_contract
    from provider_name_cut import transcript_tokens
    from timing_evidence import word_labels

    if row.get("native_source") and not row.get("stock_path"):
        if (row.get("native_reference_hold") or not row.get("reference_contract")
                or not row.get("request_reference_evidence")):
            raise OriginalSpokenNotCatalogued(row)
        return
    if not row.get("stock_path") or row.get("native_source"):
        raise ValueError(LEGACY_MISSING_SPOKEN)
    if stock_missing and row.get("analysis_base") and row.get("reference_contract"):
        # This plan carries an independent aligned playback waveform.
        return
    signed = stock_decode_contract(row)
    def verified_playback(kind):
        from lite_portable_constraints import name_occurrences

        plans = getattr(members, "verified_name_playback", {})
        proof = plans.get(digest(canonical(row))) if plans else None
        if (not proof or proof.get("kind") != kind or not row.get("paced_base_analysis")
                or proof.get("checked_seed") != row.get("companion_seed", {}).get("manifest_sha256")
                or proof.get("fingerprints") != [signed["pcm_sha256"], row["pcm_sha256"]]
                or proof.get("cuts") != [slot["source_cut"] for slot in name_occurrences(row)]):
            return False
        if playback is not None and (not playback or playback[2]["clock_contract_sha256"] != proof.get("checked_seal")
                or [part["source_samples"] for part in playback[1]["sequence"] if part["kind"] == "name"] != proof["cuts"]):
            raise ValueError("The installed playback clock differs from the one verified for this name.")
        return True

    if (signed["samples"], signed["pcm_sha256"]) != (row["samples"], row["pcm_sha256"]):
        if row.get("analysis_base") and verified_playback("paired measured playback"):
            return
        raise ValueError("This dialogue needs playback clips cut from its original Spoken recording before Fish Audio can fetch its name.")
    if word_labels(transcript_tokens(recorded_stock_transcript(row, members))) != word_labels(transcript_tokens(row["stock_text"])):
        verified = VERIFIED_NAME_CONTEXTS.get(row.get("source_id"))
        if (not verified or digest(canonical(row)) != verified) and not verified_playback("unique recorded name context"):
            raise ValueError("This dialogue's playback transcript needs to match the original Spoken recording before Fish Audio can fetch its name.")


def supplemental_stock_voice(row, members):
    """Get the archived casting for an older plan that recorded only its Spoken tail."""
    member = "references/voice-defaults.json"
    binding = row["reference_contract"]["sha256"]
    if member not in members:
        raise ValueError("The prepared voice pack needs a default reference for this dialogue.")
    raw = members[member]
    if digest(raw) not in {
        "95d9a9970bfb5c05042d2f639416001b7d405c1dd49a22a793bac41d2562eb6c",
        "a5206e87d3c2639c6dcaec9a7f9c58194c5329eb7cbf44322f9a696e72aef7f4",
    }:
        raise ValueError("The default voice casting table differs from the verified catalog.")
    catalog = json.loads(raw)
    if (not isinstance(catalog, dict) or catalog.get("format") != "dmspeaks-stock-voice-defaults-1"
            or not isinstance(catalog.get("plans"), dict) or not isinstance(catalog.get("voices"), dict)):
        raise ValueError("The prepared voice pack has an invalid casting table.")
    record = catalog["plans"].get(binding)
    if (not isinstance(record, dict) or (record.get("source_id"), record.get("npc_id")) !=
            (row["source_id"], row["npc_id"])):
        raise ValueError("The prepared voice pack has no matching casting for this dialogue.")
    voice = catalog["voices"].get(record.get("voice"))
    if (not isinstance(voice, dict) or not isinstance(voice.get("media"), dict)
            or not isinstance(voice.get("member"), str) or not isinstance(voice.get("transcript"), str)):
        raise ValueError("The prepared voice pack has an incomplete casting for this dialogue.")
    audio = unpack_wave(voice["media"], getattr(members, "reference_media", members.__getitem__)(voice["member"]))
    text = voice["transcript"]
    if (voice["media"]["wav_sha256"] != record["voice"] or not text.strip()
            or digest(text.encode()) != voice["transcript_sha256"]
            or voice["member"] != "references/media/" + voice["media"]["flac_sha256"] + ".flac"):
        raise ValueError("The default voice audio or transcript differs from its archived casting.")
    return dict(id=row["source_id"] + ":voice", transcript=text, wav_bytes=audio, sha256=record["voice"])


def hosted_stock_reference(row, stock, scratch, members, *, decoded_original=None):
    """Keep the voice first and append the entire available original performance."""
    from lite_portable_inputs import decode_stock, recorded_stock_transcript, stock_decode_contract, wave_bytes

    require_hosted_playback_base(row, stock_missing=stock is None, members=members)
    if row.get("native_source"):
        # The actor reference and any independently admitted original recording
        # have their own transcripts and clocks, separate from the playback base.
        references, _ = verified_references(row, members)
        if (any(part.get("wav_bytes") is None for part in references)
                or reference_evidence(references) != row["request_reference_evidence"]):
            raise ValueError("The ordered audio or transcripts differ from this dialogue's verified references.")
        return references
    references, contract = verified_references(row, members, defer_stock=True)
    if len(references) == 2 and contract["upstream"]["contract"]["ordered_parts"][0]["kind"] in PREFIX_KINDS:
        voice = references[:1]
        if voice[0]["wav_bytes"] is None:
            if stock is None and references[-1]["wav_bytes"] is None:
                raise OriginalSpokenNotCatalogued(row)
            raise ValueError("The prepared NPC voice prefix for this dialogue is missing.")
        if reference_evidence(voice) != row["request_reference_evidence"][:1]:
            raise ValueError("The default NPC voice differs from its verified recording.")
    else:
        voice = [supplemental_stock_voice(row, members)]
    signed = stock_decode_contract(row)
    transcript = recorded_stock_transcript(row, members)
    if stock is None:
        dialogue = references[-1]
        if (dialogue["wav_bytes"] is not None and dialogue["transcript"] == transcript
                and digest(wave_data(dialogue["wav_bytes"])[2]) == signed["pcm_sha256"]):
            return [*voice, dialogue]
        return voice
    if digest(stock.read_bytes()) != row["stock_sha256"]:
        raise ValueError("Install the matching signed Spoken recording in the selected AddOns folder.")
    reuse_decoded = decoded_original is not None and not row.get("analysis_base") and not row.get("signed_reference_parent")
    if not reuse_decoded:
        decoded_original = scratch / "fish-original-spoken.wav"
        decode_stock(stock, signed, decoded_original)
    audio = decoded_original.read_bytes()
    if reuse_decoded:
        import numpy as np

        _, _, pcm = wave_data(audio)
        if (len(pcm), digest(pcm)) != (2 * signed["samples"], signed["pcm_sha256"]):
            raise ValueError("The decoded reference differs from the original Spoken sample clock.")
        audio = wave_bytes(np.frombuffer(pcm, dtype="<i2"))
    if row.get("recorded_stock_opening") and digest(audio) != row["recorded_stock_opening"]["original_wave_sha256"]:
        raise ValueError("The installed Spoken recording does not reproduce the measured opening.")
    return [*voice, dict(id=row["source_id"] + ":dialogue", transcript=transcript,
                         wav_bytes=audio, sha256=digest(audio))]
