"""Companion adapter: extend a sealed paced base during normal Name fetching."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import json
import re
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile,ZIP_STORED

import numpy as np
import soundfile as sf
from audio import VORBIS_PAGE_OPTIONS

from lite_delivery_unit import prepare_unit
from lite_clip_bounds import boundary_method, checked_cut, clip_bounds
from lite_expanded_context import FORMAT as EXPANDED_FORMAT
from lite_paced_delivery import SETTING,BUNDLE,canonical,validate_projection,prepare_addition
from lite_portable_io import read,save,file_hash,digest,hold,filesystem_path
from lite_portable_inputs import exact_paced_stock_wave, read_paced_base_analysis, render_inputs
from lite_portable_references import references_for_stock
from lite_portable_constraints import leading_word_count, name_occurrences
from lite_portable_render import NameCutUnavailable,checked_name_article,name_alignment_text,name_text,count,provider_generation_observation
from lite_portable_constraints import name_fallback_article_changed
from lite_portable_join import (
    QUIET_CLOCK_MARGIN, expanded, phone_candidates, quiet_boundary, quiet_cut, trim_donor, splice,
)
import phoneme_alignment
from phoneme_alignment import UNKNOWN_PHONES,lexical_text,validate
from lite_source_identity import dialogue_source_tuple, source_key,signed_binding
from provider_name_cut import extra_fallback_words,transcript_tokens
from vorbis_pcm import measure_vorbis, single_stream_end


def choice_receipt(occurrence, native, paced):
    return dict(occurrence=occurrence, request_sha256=native.get("request_sha256"),
                donor_sha256=native.get("donor_sha256"), native_ogg_sha256=native.get("sha256"),
                paced_ogg_sha256=paced.get("sha256"))


def managed_name_matches(native, paced, extension, value, canonical_native):
    native_parts = {part["occurrence"]: part["choices"] for part in native["sequence"] if part["kind"] == "name"}
    paced_parts = {part["occurrence"]: part["choices"] for part in paced["sequence"] if part["kind"] == "name"}
    if (not native_parts or native_parts.keys() != paced_parts.keys()
            or any(value not in choices or value not in paced_parts[index]
                   for index, choices in native_parts.items())):
        return False
    if extension.get("actual_name_manifest_canonical_sha256") == canonical_native:
        return True
    receipts = extension.get("name_receipts", {}).get(value)
    return receipts == [choice_receipt(index, native_parts[index][value], paced_parts[index][value])
                        for index in sorted(native_parts)]


def matching_source_metadata(folder, seal, row):
    raw = (folder / "native-selection-metadata.json").read_bytes()
    if digest(raw) != seal["native_selection_metadata_sha256"]:
        raise ValueError("The sealed playback source changed.")
    metadata = json.loads(raw)
    original = metadata.get("original_spoken") or metadata.get("reference_spoken") or {}
    if row.get("stock_path") and original.get("path") and row["stock_path"] != original["path"]:
        return None
    if row.get("stock_sha256") and original.get("sha256") and row["stock_sha256"] != original["sha256"]:
        return None
    if (row["source_id"].startswith("q:") and row.get("source_text_sha256")
            and digest(metadata["original_text"].encode()) != row["source_text_sha256"]):
        return None
    return metadata


def dialogue_seals(state, row):
    key = row["source_id"], row["npc_id"]
    index = state.get("seals_by_dialogue")
    if index is not None:
        matches = index.get(key, ())
        if (addons := state.get("coverage_addons")) is not None:
            from lite_store_playback import safe_path
            return [(path, seal) for path, seal in matches
                    if safe_path(addons, Path(path).relative_to(addons)).is_dir()]
        return matches
    return ((path, seal) for path, seal in state.get("seals", {}).items()
            if (seal.get("source_id"), seal.get("npc_id")) == key)


def require_name_extension(app, row, state=None):
    state = state if state is not None else app.store.setting(SETTING,{}) or {}
    if any(seal.get('clock_format') == EXPANDED_FORMAT
           and matching_source_metadata(filesystem_path(path), seal, row) is not None
           for path, seal in dialogue_seals(state, row)):
        raise ValueError('This dialogue has prepared fallback audio. Personalized names need a verified expanded-context return.')


def prepared_base(app,row,state=None,*,verify_audio=True,giver_types=None):
    if not row.get('companion_seed'):return None
    state=state if state is not None else app.store.setting(SETTING,{}) or {}
    source_id, npc_id, locale = dialogue_source_tuple(row)
    matches=[]
    for path,seal in dialogue_seals(state, row):
        folder=filesystem_path(path);m=read(folder/'dialogue.json')
        if dialogue_source_tuple(m) != (source_id,npc_id,locale):continue
        metadata=matching_source_metadata(folder, seal, row)
        if metadata is None:continue
        if giver_types is not None and metadata.get("giver_type") not in giver_types:
            continue
        if row.get("game_object_reference") and metadata.get("giver_type") != "GameObject":
            raise ValueError("The signed object reference needs its matching game object route.")
        if "giver_type" in row and metadata.get("giver_type") != row["giver_type"]:
            raise ValueError("The signed reference needs its matching quest giver type.")
        native_manifest = m['source_native_manifest_sha256']
        if metadata.get('native_manifest_sha256', native_manifest) != native_manifest:
            raise ValueError('The sealed native source identity differs from the delivery clock.')
        extension=metadata.get('personalized_source_extension',{})
        original=(extension.get('root_native_manifest_sha256')
                  or extension.get('base_native_manifest_sha256') or native_manifest)
        if original!=row.get('companion_seed',{}).get('manifest_sha256'):
            raise ValueError('The paced base belongs to another native companion.')
        source_wave=(row.get('source_wave_sha256')or row.get('analysis_base',{}).get('wave_sha256')
                     or row.get('stock_wav_sha256'))
        if (metadata['source_wave_sha256']!=source_wave
                or metadata['source_samples']!=row['samples']
                or digest(metadata['original_text'].encode())!=row['source_text_sha256']):
            raise ValueError('The paced base does not match this exact native source recording.')
        # Coverage checks the same sealed bytes without decoding audio. A
        # fetch still verifies the full decoded sample clock before submission.
        validate_projection(folder,m,seal,verify_pcm=verify_audio)
        matches.append((folder,m,seal,metadata))
    if len(matches)>1:raise ValueError('Multiple paced bases own this native dialogue.')
    return matches[0] if matches else None


def preflight_name_base(app, row, state=None, *, verify_audio=True):
    """Reject an incompatible installed base before requesting a return."""
    from lite_portable_inputs import reconstructed_caption
    from lite_source_identity import quest_wording_key

    state = state if state is not None else app.store.setting(SETTING, {}) or {}
    require_name_extension(app, row, state)
    matched = prepared_base(app, row, state, verify_audio=verify_audio)
    if matched:
        folder, manifest, seal, _ = matched
        if not seal.get("caption_sha256") or not (folder / "caption.json").is_file():
            raise ValueError("This installed playback dialogue needs its paired caption before fetching names.")
        if row.get("requires_paced_base"):
            from lite_source_identity import signed_binding
            names = [part["source_samples"] for part in manifest["sequence"] if part["kind"] == "name"]
            cuts = [slot["source_cut"] for slot in name_occurrences(row)]
            expected_stock = None if "analysis_base" in row else signed_binding(row)
            allowed_parts = {"fixed", "name"}
            if "paced_base_analysis" in row:
                allowed_parts.update({"class", "race", "gender"})
            if (manifest["original_spoken"] != expected_stock or names != cuts
                    or any(part["kind"] not in allowed_parts for part in manifest["sequence"])):
                raise ValueError("The paced route changed the signed source or Name cut.")
        if "paced_base_analysis" in row:
            verify_base_analysis_clock(row["paced_base_analysis"], read(folder / "clock-contract.json"), row["samples"], row=row)
        return matched
    if row.get("requires_paced_base"):
        raise ValueError("Install this Name plan's exact audited playback collection before fetching names.")

    source, npc, locale = dialogue_source_tuple(row)
    matching = [(filesystem_path(path), seal) for path, seal in dialogue_seals(state, row)]
    if not matching:
        return False
    caption = reconstructed_caption(row) if source.startswith("q:") else None
    wording = quest_wording_key({**row, "reconstructed_caption": caption})
    for path, seal in matching:
        data = (path / "native-selection-metadata.json").read_bytes()
        if digest(data) != seal["native_selection_metadata_sha256"]:
            raise ValueError("The sealed playback source changed.")
        route = json.loads(data)
        if route["locale"] == locale and (wording is None or wording == quest_wording_key(route)):
            raise ValueError("This installed playback dialogue needs a Name plan prepared from its exact source.")
    return False


def caption_onsets(words, display, start, stop, lead):
    """Project measured spoken words onto the delivered clip's sample clock."""
    if not words:
        return None
    if any(not word["phonemes"] or any(phone["phone"].casefold() in UNKNOWN_PHONES
               for phone in word["phonemes"]) for word in words):
        return None
    starts = [word["start_sample"] for word in words]
    if starts[0] < start:
        quiet = lead.get("return_waveform_quiet_samples")
        if not (quiet and quiet[0] <= start <= quiet[1] and lead["return_cut_sample"] == start
                and start < words[0]["phonemes"][0]["end_sample"]):
            return None
        starts[0] = start
    if not all(start <= onset < stop for onset in starts) or starts != sorted(starts):
        return None
    display_words = display.split()
    if len(words) != len(display_words):
        widths = [count(word) for word in display_words]
        if not widths or not all(widths) or sum(widths) != len(words):
            return None
        starts = [starts[index] for index in np.cumsum([0, *widths[:-1]])]
    return [onset - start for onset in starts]


def name_verification_input(words, leader_count, name_count, start, stop, slot):
    """Describe the spoken name on the delivered Ogg's clock, independent of caption spelling."""
    if name_count < 1 or len(words) != leader_count + name_count:
        return None
    name_words = words[leader_count:]
    if any(not word["phonemes"] for word in name_words):
        return None
    first_phone = name_words[0]["phonemes"][0]
    onset = max(start, first_phone["start_sample"])
    if onset >= min(first_phone["end_sample"], stop):
        return None
    spoken = [word["word"] for word in words[:leader_count]]
    name = "-".join(word["word"] for word in name_words)
    if name_count > 1:
        # A spoken alias like “Flay-Hew” occupies one name slot. Compare the
        # whole pronunciation with the placeholder at the first syllable.
        phones = [phone["phone"] for word in name_words for phone in word["phonemes"]]
        if any(phone.casefold() in UNKNOWN_PHONES for phone in phones):
            return None
        slot = {**slot, "pronunciations": {**(slot.get("pronunciations") or {}), name.casefold(): phones}}
    return [*spoken, name], onset - start, slot


def cuts_clear_of_reported_fallback(selections, exclusion, request_id):
    first, last = exclusion["name_samples"]
    clean = [option for option in selections if option[2][0] <= first and last <= option[2][1]
             and not any(option[2][0] < end and begin < option[2][1]
                         for begin, end in exclusion["paced_samples"])]
    if not clean:
        raise NameCutUnavailable("The paced Name has no measured cut clear of the reported Adventurer speech.",
                                 request_id=request_id)
    return clean


def measure_paced_fallback(raw, receipt, donor, transcript, span, excluded, align_factory, scratch, slot, leader):
    """Measure an observed extra word on the transformed waveform's own clock."""
    raw_asset = dict(sha256=receipt["wav_sha256"], sample_rate=44100, sample_count=sf.info(raw).frames)
    observed, _ = provider_generation_observation(raw_asset, receipt.get("word_timing_evidence"))
    extras = extra_fallback_words(transcript, span, observed, raw_asset["sample_count"])
    if [item["samples"] for item in extras if item["near_name"]] != excluded:
        raise ValueError("The saved return no longer establishes the reported Adventurer intervals.")

    original = transcript_tokens(transcript)
    measured, measured_span = name_alignment_text(transcript, span)
    normalized = transcript_tokens(measured)
    tokens = [item["word"] for item in normalized]
    offsets = [
        count(transcript[:original[item["position"]]["first"]])
        if item["position"] < len(original) else len(normalized)
        for item in extras
    ]
    for index in reversed(offsets):
        tokens.insert(index, "Adventurer")
    expanded_transcript = " ".join(tokens)
    derived = dict(sha256=donor["derived_sha256"], sample_rate=44100, sample_count=donor["derived_samples"])
    unit_folder = Path(donor["audio"]).parent
    with hold(unit_folder, ".pace-unit.lock"):
        path = phoneme_alignment.evidence_path(unit_folder, derived, expanded_transcript)
        if path.is_file():
            evidence = phoneme_alignment.read(unit_folder, derived, expanded_transcript)
        else:
            align = align_factory(expanded_transcript, scratch, slot)
            evidence = align(donor["audio"], derived, expanded_transcript)
            phoneme_alignment.write(unit_folder, derived, expanded_transcript, evidence)
        words = validate(evidence, derived, expanded_transcript)
        alignment_sha = file_hash(path)
    if [lexical_text(word["word"]) for word in words] != [lexical_text(token) for token in tokens]:
        raise ValueError("The paced fallback alignment does not preserve the observed word order.")
    excluded_indices = {offset + index for index, offset in enumerate(offsets)}
    excluded_words = [word for index, word in enumerate(words) if index in excluded_indices]
    retained = [word for index, word in enumerate(words) if index not in excluded_indices]
    indices = [index for index, item in enumerate(normalized)
               if measured_span[0] <= item["first"] < item["last"] <= measured_span[1]]
    name_words = [retained[index] for index in indices]
    if not name_words or any(not word["phonemes"] or any(phone["phone"].casefold() in UNKNOWN_PHONES
                       for phone in word["phonemes"]) for word in [*excluded_words, *name_words]):
        raise ValueError("The paced alignment could not measure the Name and extra Adventurer phonemes.")
    proof = dict(provider_samples=excluded, paced_samples=[[word["start_sample"], word["end_sample"]]
                 for word in excluded_words], name_samples=[name_words[0]["phonemes"][0]["start_sample"],
                 name_words[-1]["phonemes"][-1]["end_sample"]], alignment_sha256=alignment_sha,
                 transcript_sha256=evidence["transcript_sha256"])
    return proof, retained[max(0, indices[0] - leader):indices[-1] + 1]


def admitted_paced_base_unit(clock, native_samples):
    sequence = clock["sequence"]
    fixed_units = {part["choices"]["fixed"]["unit"] for part in sequence if part["kind"] == "fixed"}
    if len(fixed_units) == 1:
        return next(iter(fixed_units))
    if len(sequence) == 1:
        part, = sequence
        # When the Name occupies the entire recording, the admitted
        # original Adventurer choice supplies the base unit identity.
        if (part["kind"] == "name" and part["fallback"] == "adventurer"
                and part["native_span"] == [0, native_samples] and part["derived_span"][0] == 0):
            fallback = part["choices"]["adventurer"]
            if fallback["clock"] == "decoded_ogg_pcm":
                return fallback["unit"]
    raise ValueError("The admitted paced clock has no unique base unit.")


def signed_whole_phrase_base_unit(clock, row):
    """Derive the original paced base for a signed, entirely replaced Name phrase."""
    if (not row or row.get("analysis_base") or row.get("native_source")
            or row.get("requires_paced_base") is not True
            or not row.get("leading_word") or not row.get("stock_wav_sha256")
            or row.get("source_cut") != [0, row["samples"]]
            or clock["native_manifest_sha256"] != row.get("companion_seed", {}).get("manifest_sha256")):
        return None
    sequence = clock["sequence"]
    if (len(sequence) != 1 or sequence[0]["kind"] != "name"
            or sequence[0]["native_span"] != [0, row["samples"]]
            or clock["delivery_profile"] != {"numerator": 16, "denominator": 15}):
        return None
    from lite_portable_join import expanded
    utterance = expanded(row["stock_text"]).strip()
    return digest(f'{row["stock_wav_sha256"]}\n{utterance}\n16/15'.encode())


def verify_base_analysis_clock(descriptor, clock, native_samples, *, row=None):
    admitted = admitted_paced_base_unit(clock, native_samples)
    if (descriptor["unit"] not in {admitted, signed_whole_phrase_base_unit(clock, row)}
            or clock["sequence"][-1]["derived_span"][1] != descriptor["samples"]):
        raise ValueError("The measured paced analysis belongs to a different admitted base.")
    if "join_windows" in descriptor:
        spans = [part["derived_span"] for part in clock["sequence"] if part["kind"] == "name"]
        if descriptor["join_windows"]["spans"] != spans:
            raise ValueError("The measured join windows belong to different paced Name cuts.")


def name_caption(fallback, name, slot, article, leader):
    """Fill the admitted corpus occurrence and preserve its authored punctuation."""
    start, end = slot["slot"]
    original = slot["stock_text"][start:end]
    words = [match for match in re.finditer(r"\S+", fallback) if lexical_text(match[0])]
    if not original or len(words) != leader + 1:
        raise ValueError("The paced Name caption differs from its corpus occurrence.")
    token = words[leader]
    suffix = slot.get("name_suffix", "")
    if lexical_text(token[0]) != lexical_text(original + suffix):
        raise ValueError("The paced Name caption differs from its corpus occurrence.")
    occurrence = re.search(rf"\b{re.escape(original)}\b", token[0], flags=re.I)
    if occurrence is None:
        raise ValueError("The paced Name caption differs from its corpus occurrence.")
    left = token.start() + occurrence.start()
    right = token.start() + occurrence.end()
    personalized = fallback[:left] + name + fallback[right:]
    spoken = article["spoken_text"] if article else None
    if spoken is None and name_fallback_article_changed(slot):
        spoken = slot["leading_word"]["source_text"]
    if spoken:
        prior = words[leader - 1] if leader else None
        match = re.fullmatch(r"[Aa][Nn]?", prior[0]) if prior else None
        if not match or not fallback[prior.end():token.start()].isspace():
            raise ValueError("The paced Name caption needs the measured leading article.")
        replacement = spoken.capitalize() if prior[0][0].isupper() else spoken.casefold()
        personalized = personalized[:prior.start()] + replacement + personalized[prior.end():]
    return personalized


def paced_leading_words(binding, fallback, start, slot, article, base, returned, first, last, donor_first,
                        *, fallback_clip=None, base_unit=None, source_audio=None, caption_text=None,
                        measured_leading_words=None):
    """Admit extra leading speech only when the sealed fallback already owns it."""
    native_leader = leading_word_count(slot)
    caption = [lexical_text(word) for word in binding["choices"][fallback].split()]
    left, right = slot["slot"]
    corpus_name = lexical_text(slot["stock_text"][left:right] + slot.get("name_suffix", ""))
    if not caption or caption[-1] != corpus_name or len(caption) <= native_leader + 1:
        return native_leader

    leader = len(caption) - 1
    source_first = first - leader
    return_first = donor_first - leader
    measured_prefix = measured_leading_words == leader and source_audio is not None and fallback_clip is not None
    if ((not native_leader and not measured_prefix) or article or corpus_name in caption[:-1]
            or source_first < 0 or return_first < 0):
        raise ValueError("The paced Name prefix differs from its verified source context.")
    source_words = base[source_first:last]
    returned_prefix = returned[return_first:donor_first]
    starts = [word["start_sample"] - start for word in source_words]
    measured = binding.get("delivered_word_samples", {}).get(fallback)
    separate_fallback_unit = (fallback_clip is not None and base_unit is not None
                              and fallback_clip["unit"] != base_unit)
    if separate_fallback_unit:
        # A sealed older fallback may have been cut from a different recording.
        # Its caption uses that Ogg's clock. The source's phoneme clock still
        # determines lexical ownership and the location of the new splice.
        caption_clock_matches = (isinstance(measured, list) and len(measured) == len(starts)
                                 and all(type(sample) is int for sample in measured)
                                 and bool(measured) and measured[0] >= 0
                                 and all(a < b for a, b in zip(measured, measured[1:]))
                                 and measured[-1] < fallback_clip["samples"])
    else:
        caption_clock_matches = measured == [max(0, sample) for sample in starts]
    positions_match = (binding["first"], binding["last"]) == (source_first + 1, last)
    if not positions_match and caption_text is not None:
        tokens = caption_text.split()
        left, right = binding["first"] - 1, binding["last"]
        positions_match = (0 <= left < right <= len(tokens) and count(" ".join(tokens[:left])) == source_first
                           and count(" ".join(tokens[:right])) == last
                           and [lexical_text(word) for word in tokens[left:right]] == caption)
    if (not positions_match or not starts
            or not caption_clock_matches
            or [lexical_text(word["word"]) for word in source_words] != caption
            or [lexical_text(word["word"]) for word in returned_prefix] != caption[:-1]):
        raise ValueError("The paced Name prefix differs from its verified source context.")
    if starts[0] < 0:
        # Some sealed cuts lie inside the aligner's first interval because
        # that interval includes quiet before the word. Prove the exact cut.
        gap = quiet_boundary(source_audio, base, source_first) if source_audio is not None and source_first else None
        if not gap or (quiet_cut(gap, "before") != start and not (
            start == gap[2] and gap[0] + QUIET_CLOCK_MARGIN <= start <= gap[1] - QUIET_CLOCK_MARGIN
        )):
            raise ValueError("The paced Name prefix differs from its verified source context.")
    return leader


def stage_name(app,row,native_folder,name,spoken,provider,members,queue,status,align_factory,*,state=None,stock=None,
               archived_returns=None, publish_store_name=None, on_verify_clip=None, seed_joins=None, only_occurrence=None,
               store_addons=None, check_placeholder=None):
    """Return a privately prepared cumulative state, or None without a paced base.

    Native rendering remains the request/receipt authority. The adapter requires
    its complete saved return and the native base, measures both paced clocks,
    and may add only the already declared contextual words at existing cuts.
    """
    require_name_extension(app,row,state)
    matched=prepared_base(app,row,state)
    if matched is None:return None
    folder,current,seal,metadata=matched
    native_path=native_folder/'dialogue.json';native=read(native_path)
    canonical_native=digest(canonical(native));extension=metadata.get('personalized_source_extension',{})
    value=name.casefold()
    slots=name_occurrences(row)
    if managed_name_matches(native,current,extension,value,canonical_native):
        return state if state is not None else app.store.setting(SETTING,{})
    clock=read(folder/'clock-contract.json');caption=read(folder/'caption.json')
    work=app.folder/'spoken-player-names';unit_cache=work/'paced-units'
    requests=[];proofs=[];payloads={};receipts=[];article_repairs=[]
    decoded = {}
    prior_receipts = {item["occurrence"]: item for item in extension.get("name_receipts", {}).get(value, [])}

    def delivery_audio(clip):
        filename = clip["file"]
        if filename not in payloads:
            path = folder / filename
            if publish_store_name is not None:
                from lite_store_playback import stored_clip_path

                path = stored_clip_path(folder, seal, filename)
            payloads[filename] = path.read_bytes()
        audio = payloads[filename]
        if digest(audio) != clip["sha256"]:
            raise ValueError("A retained paced clip changed.")
        return audio

    def delivery_pcm(clip):
        filename = clip["file"]
        audio = delivery_audio(clip)
        if filename not in decoded:
            # Decode from the first frame, as WoW does. Seeking to the last
            # frame of a Vorbis file can return a different boundary sample.
            with sf.SoundFile(BytesIO(audio)) as source:
                if (source.samplerate, source.channels, source.frames) != (44100, 1, clip["samples"]):
                    raise ValueError("The paced clip changed its playback clock.")
                samples = source.read(dtype="float32")
                if len(samples) != clip["samples"]:
                    raise ValueError("The paced clip did not decode to its verified duration.")
                decoded[filename] = samples
        return decoded[filename]

    def fixed_endpoint(index, tail, default):
        if not 0 <= index < len(clock["sequence"]):
            return default
        part = clock["sequence"][index]
        if part["kind"] != "fixed":
            return default
        samples = delivery_pcm(part["choices"]["fixed"])
        return samples[-1 if tail else 0]

    with TemporaryDirectory(dir=work,prefix='.paced-name-') as temporary:
        scratch = Path(temporary)
        if publish_store_name is not None:
            unit_cache = scratch / 'verified-units'
        if "paced_base_analysis" in row:
            analysis = row["paced_base_analysis"]
            verify_base_analysis_clock(analysis, clock, row['samples'], row=row)
            base_pcm, base_words = read_paced_base_analysis(row, members)
            base = {"key": analysis["unit"], "derived_sha256": analysis["wave_sha256"],
                    "derived_phonemes_sha256": analysis["phones_sha256"]}
        else:
            base_wave = scratch / 'base.wav'
            render_inputs(stock,row,base_wave,members)
            # Use the verified reference for the precise aligned WAV envelope.
            if (not row.get("native_source") and not row.get("analysis_base")
                    and row.get("reference_contract")
                    and file_hash(base_wave) != row["stock_wav_sha256"]):
                references = references_for_stock(row, members, base_wave)
                base_wave.write_bytes(exact_paced_stock_wave(row, references))
            base_text=expanded(row['stock_text']).strip()
            base=prepare_unit(base_wave,base_text,row['base_phones'],unit_cache,
                              align_factory(base_text,scratch,row))
            base_pcm,rate=sf.read(base['audio'],dtype='float32');assert rate==44100
            if len(base_pcm)!=clock['sequence'][-1]['derived_span'][1]:
                raise ValueError('The complete paced base differs from the admitted delivery clock.')
            base_words = base['phones']['words']
        # Fixed media must describe the exact transformed base PCM. Reuse all
        # prior Oggs unchanged; never apply tempo to those cut components.
        window_leaders = row.get("paced_base_analysis", {}).get("join_windows", {}).get("leading_word_counts")
        for slot_index, slot in enumerate(slots):
            occurrence=slot.get('name_occurrence',1)
            if only_occurrence is not None and occurrence != only_occurrence:
                continue
            index=next(i for i,p in enumerate(current['sequence']) if p['kind']=='name' and p['occurrence']==occurrence)
            old=clock['sequence'][index];owner=metadata['occurrences'][index]
            native_part=next(p for p in native['sequence'] if p['kind']=='name' and p['occurrence']==occurrence)
            item=native_part['choices'][value]
            if "name_article" in item:
                slot = {**slot, "name_article": item["name_article"]}
            article = checked_name_article(slot)
            previous_clip = old["choices"].get(value)
            if previous_clip and choice_receipt(occurrence, item, previous_clip) == prior_receipts.get(occurrence):
                receipts.append(prior_receipts[occurrence])
                continue
            repairing_article = previous_clip is not None and article and item.get("published_file")
            if previous_clip and not repairing_article:
                raise ValueError("A different paced take already owns this Name.")
            if on_verify_clip:
                on_verify_clip(slot)
            request_text,request_span=name_text(slot,spoken)
            transcript,span=name_alignment_text(request_text,request_span)
            if item.get('provider_id')!=provider or item.get('reference_contract_sha256')!=slot.get('reference_contract',{}).get('sha256'):
                raise ValueError('The saved native Name uses another provider or voice-reference contract.')
            if item['measured_name']['transcript_sha256']!=digest(transcript.encode()):
                raise ValueError('The saved native Name has another spoken form.')
            request_id=item['request_sha256']
            if not re.fullmatch('[0-9a-f]{64}',request_id):raise ValueError('Invalid returned request identity.')
            archived = (archived_returns or {}).get(request_id)
            if archived is not None:
                evidence, content = archived
                if (evidence["source_id"] != row["source_id"]
                        or evidence.get("name_occurrence") != slot.get("name_occurrence")
                        or evidence["spoken_transcript"] != request_text
                        or digest(content) != evidence["wav_sha256"]
                        or evidence["wav_sha256"] != item["donor_sha256"]):
                    raise ValueError("The imported return differs from the measured native Name.")
                raw = scratch / f"saved-return-{occurrence}.wav"
                raw.write_bytes(content)
                receipt = {"wav_sha256": evidence["wav_sha256"]}
                phones = evidence["phones"]
            else:
                history = queue / request_id
                raw, receipt_path = history / "return.wav", history / "receipt.json"
                receipt = read(receipt_path) if receipt_path.is_file() else {}
                if (receipt.get("state") != "returned" or not raw.is_file()
                        or file_hash(raw) != receipt.get("wav_sha256") or file_hash(raw) != item["donor_sha256"]
                        or receipt.get("transcript") != request_text):
                    raise ValueError("Final pacing needs the complete saved contextual return; no new speech request was sent.")
                phones = read(history / "phones.json")
            donor=prepare_unit(raw,transcript,phones,unit_cache,align_factory(transcript,scratch,slot))
            returned,_=sf.read(donor['audio'],dtype='float32');words=donor['phones']['words']
            lo,hi=slot['context'];left,right=slot['slot']
            begin,end,first,last=[count(slot['stock_text'][:x]) for x in (lo,hi,left,right)]
            donor_first,donor_last=[count(transcript[:x]) for x in span]
            starts,ends=phone_candidates(returned,words,0,len(words),donor_first,donor_last)
            trim_donor(returned,words,starts,ends)
            candidate=dict(phones=words,samples=returned,starts=starts,ends=ends,begin=begin,
                           extra=donor_last-donor_first-(last-first))
            binding=next(p for p in caption['bindings'] if p['sequence_index_1based']==index+1)
            leader = paced_leading_words(binding, old["fallback"], old["derived_span"][0], slot, article,
                                        base_words, words, first, last, donor_first,
                                        fallback_clip=old["choices"][old["fallback"]], base_unit=base["key"],
                                        source_audio=base_pcm, caption_text=caption["text"],
                                        measured_leading_words=window_leaders[slot_index] if window_leaders is not None else None)
            if window_leaders is not None and window_leaders[slot_index] != leader:
                raise ValueError("The compact join window differs from the sealed leading words.")
            selections=splice(base_pcm,base_words,candidate,begin,end,first,last,old['derived_span'],
                               name_only=not leader,existing_leader=leader,
                               article={"source": article["source"], "spoken": article["spoken_text"]} if article else None)
            if not selections:
                raise NameCutUnavailable("The returned name could not be isolated on the paced recording.",
                                         request_id=request_id)
            exclusion = None
            caption_words = []
            if (leader <= donor_first < donor_last <= len(words)
                    and words[donor_first]['char_start'] == span[0]
                    and words[donor_last - 1]['char_end'] == span[1]):
                caption_words = words[donor_first - leader:donor_last]
            if excluded := item.get("excluded_provider_fallback_samples"):
                exclusion, caption_words = measure_paced_fallback(raw, receipt, donor, request_text, request_span, excluded,
                                                                  align_factory, scratch, slot, leader)
                selections = cuts_clear_of_reported_fallback(selections, exclusion, request_id)
            _,source,take,joins=selections[0];a,b=take;start,stop=source
            # The owned context and the name share one performance. Keep every
            # sample between the two outer cuts, including any connecting speech.
            raw_clip = returned[a:b].copy()
            width=round(44100*.003)
            if len(raw_clip)<2*width:raise ValueError('The measured Name is too short for its declared edge join.')
            ramp=np.linspace(0,1,width,dtype=np.float32)
            # Join the new clip to the decoded Oggs that WoW actually plays.
            # Variable neighbors keep the measured source anchor and choice checks.
            before = fixed_endpoint(index - 1, True, base_pcm[start - 1] if start else 0)
            after = fixed_endpoint(index + 1, False, base_pcm[stop] if stop < len(base_pcm) else 0)
            raw_clip[:width]=before*(1-ramp)+raw_clip[:width]*ramp
            raw_clip[-width:]=after*(1-ramp[::-1])+raw_clip[-width:]*ramp[::-1]
            output=scratch/item.get('published_file',item['file'])
            subprocess.run(['ffmpeg','-nostdin','-v','error','-n','-f','f32le','-ar','44100','-ac','1','-i','pipe:0',
                '-c:a','libvorbis','-q:a','6',*VORBIS_PAGE_OPTIONS,str(output)],input=np.ascontiguousarray(raw_clip,dtype='<f4').tobytes(),check=True,capture_output=True,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            frames, rate, channels = measure_vorbis(output)
            granule_frames = single_stream_end(output)
            if (frames, rate, channels) != (len(raw_clip), 44100, 1) or granule_frames != len(raw_clip):
                raise ValueError('The paced Name changed its decoded clock.')
            # Both decoders must deliver the complete clip before exposing it to WoW.
            ff=subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(output),'-f','f32le','-ar','44100','-ac','1','-'],check=True,capture_output=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
            if len(ff) != 4 * frames:
                raise ValueError('The game and editing decoders disagree on this Name clip. The clip was not installed.')
            clip=dict(file=output.name,sha256=file_hash(output),samples=len(raw_clip),clock='decoded_ogg_pcm',unit=donor['key'])
            clip["cut"] = clip_bounds(item["donor_sha256"], donor["derived_sha256"], len(returned), a, b,
                                      boundary_method(joins["lead"], a, trailing=False),
                                      boundary_method(joins["tail"], b, trailing=True))
            if "cut" in item:
                clip["native_cut"] = checked_cut(item["cut"], item["samples"])
            if repairing_article:
                if previous_clip["file"] != clip["file"]:
                    raise ValueError("The paced article repair needs the existing Name filename.")
                article_repairs.append(dict(occurrence=occurrence, value=value,
                                            old_sha256=previous_clip["sha256"], sha256=clip["sha256"]))
            old['choices'][value]=clip;payloads[output.name]=output.read_bytes()
            if value not in owner['choice_keys']:
                owner['choice_keys'].append(value)
                owner['expected_values'].append(name)
            binding['choices'][value] = name_caption(binding['choices'][old['fallback']], name, slot, article, leader)
            onsets = caption_onsets(caption_words, binding['choices'][value], a, b, joins['lead'])
            if check_placeholder and old['fallback'].casefold() == 'adventurer':
                check = name_verification_input(caption_words, leader, donor_last - donor_first, a, b, slot)
                if check is None:
                    raise NameCutUnavailable("The delivered recording has no measured onset for the spoken name.",
                                             request_id=request_id)
                leak = check_placeholder(output, *check)
                if leak:
                    raise NameCutUnavailable(
                        "The delivered recording contains the stock 'adventurer' sound at the player name.",
                        request_id=request_id)
            if onsets is not None:
                binding.setdefault('delivered_word_samples', {})[value] = onsets
            elif repairing_article and 'delivered_word_samples' in binding:
                binding['delivered_word_samples'].pop(value, None)
                if not binding['delivered_word_samples']:
                    del binding['delivered_word_samples']
            if leader:
                clip["context_phrase"] = dict(
                    source="returned_wav", returned_sha256=item["donor_sha256"],
                    paced_return_sha256=donor["derived_sha256"], donor_samples=[a, b],
                    words=[word["word"] for word in words[donor_first - leader:donor_last]],
                )
            requests.append(request_id)
            receipts.append(choice_receipt(occurrence,item,clip))
            proofs.append(dict(occurrence=occurrence,request_sha256=request_id,raw_sha256=item['donor_sha256'],
                derived_unit=donor['key'],derived_audio_sha256=donor['derived_sha256'],derived_phones_sha256=donor['derived_phonemes_sha256'],
                derived_samples=donor['derived_samples'],donor_samples=[a,b],source_samples=[start,stop],phoneme_joins=joins,
                clip=clip,preencode_pcm_sha256=digest(np.ascontiguousarray(raw_clip,dtype='<f4').tobytes()),
                decoded_clock=dict(vorbis_granule_samples=granule_frames, libsndfile_samples=frames,
                                   ffmpeg_raw_samples=len(ff)//4)))
            if onsets is not None:
                proofs[-1]['caption_word_samples'] = onsets
            if exclusion is not None:
                proofs[-1]["fallback_exclusion"] = exclusion
        if not proofs:
            return state if state is not None else app.store.setting(SETTING,{})
        name_receipts = {**extension.get("name_receipts", {}), value: receipts}
        lineage=dict(base_native_manifest_sha256=current['source_native_manifest_sha256'],
            root_native_manifest_sha256=row['companion_seed']['manifest_sha256'],
            actual_name_manifest_sha256=file_hash(native_path),actual_name_manifest_canonical_sha256=canonical_native,
            source_contract_sha256=(row['native_source']['source_contract_sha256']if row.get('native_source')else digest(canonical(row))),
            generation_source_key=source_key(row),signed_stock_binding=signed_binding(row),
            name_receipts=name_receipts,
            raw_response_sha256=proofs[0]['raw_sha256'] if len(proofs)==1 else digest(canonical([p['raw_sha256'] for p in proofs])),
            request_sha256=requests[0] if len(requests)==1 else digest(canonical(requests)))
        if article_repairs:
            lineage["article_repairs"] = article_repairs
        proof=dict(format='dmspeaks-managed-paced-name-1',source_id=row['source_id'],npc_id=row['npc_id'],
            name=name,spoken_name=spoken,provider_id=provider,lineage=lineage,base_unit=base['key'],
            base_derived_audio_sha256=base['derived_sha256'],base_derived_phones_sha256=base['derived_phonemes_sha256'],
            occurrences=proofs,prior_clock_sha256=seal['clock_contract_sha256'],provider_posts=0)
        native_sha=digest(canonical(lineage));candidate_sha=digest(canonical(proof))
        metadata.update(native_manifest_sha256=native_sha,personalized_source_extension=lineage)
        clock.update(native_manifest_sha256=native_sha,candidate_sha256=candidate_sha)
        clock.pop('render_fingerprint',None);clock['render_fingerprint']=digest(canonical(clock))
        for part in clock['sequence']:
            for item in part['choices'].values():
                delivery_pcm(item)
        # A successful encode is not enough: hold newly introduced endpoint
        # steps for review, comparing the same adjacent native performance.
        changed={item['clip']['file'] for item in proofs}
        native_parts=native['sequence']
        for index,(left,right) in enumerate(zip(clock['sequence'],clock['sequence'][1:])):
            for lv,lclip in left['choices'].items():
                for rv,rclip in right['choices'].items():
                    if not {lclip['file'],rclip['file']}&changed:continue
                    lp=native_parts[index];rp=native_parts[index+1]
                    prior_left=lp['clip'] if lp['kind']=='fixed' else lp['choices'][lv]
                    prior_right=rp['clip'] if rp['kind']=='fixed' else rp['choices'][rv]
                    if seed_joins is None:
                        a=sf.read(native_folder/prior_left['file'],dtype='float32')[0][-1]
                        b=sf.read(native_folder/prior_right['file'],dtype='float32')[0][0]
                    else:
                        a = seed_joins.edge(native_folder, prior_left, "tail")[0]
                        b = seed_joins.edge(native_folder, prior_right, "head")[0]
                    prior=abs(float(a)-float(b))
                    after=abs(float(decoded[lclip['file']][-1])-float(decoded[rclip['file']][0]))
                    if after-prior>.004:
                        raise ValueError('The encoded paced Name join needs review; prior delivery and full response were retained.')
        if publish_store_name is not None:
            if store_addons is not None:
                from lite_store_playback import finish_placed_returns, pending_return_cleanup
                markers = pending_return_cleanup(store_addons, queue, seal, current["sequence"], value, proofs)
            publish_store_name(seal, current, clock, caption, value, payloads)
            if store_addons is not None:
                finish_placed_returns(store_addons, queue, markers)
            return state
        payloads.update({'clock-contract.json':canonical(clock),'native-selection-metadata.json':canonical(metadata),'caption.json':canonical(caption)})
        trust=dict(directory='delivery',ordinal=row.get('catalog_ordinal',0),source_id=row['source_id'],npc_id=row['npc_id'],
            clock_contract_sha256=digest(payloads['clock-contract.json']),native_selection_metadata_sha256=digest(payloads['native-selection-metadata.json']),
            caption_sha256=digest(payloads['caption.json']),source_native_manifest_sha256=native_sha,source_candidate_manifest_sha256=candidate_sha)
        index=canonical(dict(format=BUNDLE,packages=[trust]));archive=work/'paced-generated'/(candidate_sha+'.zip');archive.parent.mkdir(exist_ok=True)
        if not archive.exists():
            with ZipFile(archive,'x',ZIP_STORED) as z:
                z.writestr('input.json',index)
                for filename,content in payloads.items():z.writestr('delivery/'+filename,content)
        save(archive.with_suffix('.json'),proof)
        return prepare_addition(app,dict(path=str(archive),sha256=file_hash(archive),input_sha256=digest(index)),status,base_state=state)


def discard_paced_unit(app,key,expected_sha=None):
    """Retire verified temporary PCM while keeping its measured receipt."""
    work=app.folder/'spoken-player-names'
    unit=work/'paced-units'/key
    # Imported paced analyses may have no local WAV or work directory.
    if not unit.exists():
        return
    with hold(unit,'.pace-unit.lock'):
        receipt=read(unit/'unit.json');audio=unit/'coherent-16-15.wav'
        if audio.is_file():
            if (file_hash(audio)!=receipt['derived_sha256']
                    or (expected_sha is not None and receipt['derived_sha256']!=expected_sha)):
                raise ValueError('The derived audio changed; it was retained for recovery.')
            # Persist retirement before unlink so a partial failure is retryable.
            receipt['derived_discarded']=True;save(unit/'unit.json',receipt);audio.unlink()


def discard_derived_audio(app,state):
    """Retire only verified generated temporary units after successful placement."""
    work=app.folder/'spoken-player-names'
    for path,seal in state.get('seals',{}).items():
        proof_path=work/'paced-generated'/(seal['source_candidate_manifest_sha256']+'.json')
        if not proof_path.is_file():continue
        folder=filesystem_path(path);manifest=read(folder/'dialogue.json')
        validate_projection(folder,manifest,seal)
        proof=read(proof_path)
        if digest(canonical(proof))!=seal['source_candidate_manifest_sha256']:
            raise ValueError('The generated paced receipt changed; temporary WAVs were retained.')
        for key in {proof['base_unit'],*(item['derived_unit'] for item in proof['occurrences'])}:
            discard_paced_unit(app,key)
