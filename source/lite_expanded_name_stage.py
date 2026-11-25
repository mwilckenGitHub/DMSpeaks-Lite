"""Prepare an additive expanded Name package from an admitted complete return."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_STORED, ZipFile

import numpy as np
import soundfile as sf

from guide_bindings import RATE
from lite_expanded_context import BUNDLE, FORMAT as CLOCK_FORMAT
from lite_expanded_name_addition import FIELD, FORMAT, NAME, PINS
from lite_expanded_return_cut import prepare_named_cut, require, smooth_edges
from lite_paced_delivery import SETTING, canonical, prepare_addition, validate_projection
from lite_portable_io import cache_bytes, digest, file_hash, filesystem_path, hold, read, save
from lite_portable_join import join_discontinuities
from lite_portable_render import write_clip
from lite_source_identity import signed_binding
from vorbis_pcm import single_stream_end

def checked_binding(app, row, members):
    """Bind the imported preparation to the installed expanded playback."""
    from lite_expanded_binding import ANALYSIS_HOLDS, descriptor

    bound = descriptor(row, members)
    if bound is None:
        return None
    if analysis := bound.get("analysis_source"):
        raise ValueError(ANALYSIS_HOLDS[analysis["review_hold"]])
    return bind_playback(app, row, bound)


def bind_playback(app, row, bound):
    _, folder, seal, _ = parent_route(app, row, bound)
    manifest = read(folder / "dialogue.json")
    position = bound["delivered_part_index_1based"] - 1
    part = manifest["sequence"][position]
    clock = read(folder / "clock-contract.json")
    require(0 <= position < len(manifest["sequence"])
            and seal["source_candidate_manifest_sha256"] == bound["candidate_manifest_sha256"]
            and part["kind"] == "name"
            and clock["sequence"][position]["native_occurrence_index_1based"]
            == bound["native_marked_name_occurrence_1based"]
            and part["choices"][part["fallback"]]["sha256"] == bound["expanded_fallback_ogg_sha256"]
            and part["caption_binding"]["choices"][part["fallback"]] == bound["fallback_caption"],
            "The admitted expanded Name owner changed")
    return bound, part["choices"]


def generated_records(app):
    root = app.folder / "spoken-player-names/expanded-generated"
    for path in root.glob("*/verification.json"):
        record = read(path)
        if path.parent.name == digest(canonical(record["proof"])):
            yield record


def require_retained_choice(app, bound, name, transcript_sha256, provider, clip):
    """Avoid a resubmission for an already placed, exactly matching choice."""
    for record in generated_records(app):
        proof = record["proof"]
        lineage = proof["lineage"]
        if (record["clip"]["sha256"] == clip["sha256"]
                and proof["source_id"] == bound["source_id"] and proof["npc_id"] == bound["npc_id"]
                and proof["provider_id"] == provider and lineage["display_name"] == name
                and lineage["reference_contract_sha256"] == bound["reference_contract_sha256"]
                and proof["prepared_cut"]["request_transcript_sha256"] == transcript_sha256):
            return lineage["request_sha256"]
    raise ValueError("This expanded Name already has a different verified recording.")


def discard_placed_units(app, state):
    from lite_paced_name import discard_paced_unit

    for path, seal in state.get("seals", {}).items():
        folder = filesystem_path(path)
        manifest = read(folder / "dialogue.json")
        lineage = manifest.get(FIELD)
        if lineage is None:
            continue
        validate_projection(folder, manifest, seal)
        clip = manifest["sequence"][lineage["delivered_index"] - 1]["choices"][lineage["display_name"].casefold()]
        for record in generated_records(app):
            proof = record["proof"]
            if proof["lineage"] == lineage and record["clip"]["sha256"] == clip["sha256"]:
                discard_paced_unit(app, lineage["paced_unit_sha256"],
                                   proof["prepared_cut"]["returned_derived_wav_sha256"])
                break


def parent_route(app, row, bound):
    state = app.store.setting(SETTING, {}) or {}
    matches = [(filesystem_path(path), seal) for path, seal in state.get("seals", {}).items()
               if (seal.get("source_id"), seal.get("npc_id")) == (row["source_id"], row["npc_id"])]
    require(len(matches) == 1, "Expanded Name requires one admitted playback base")
    folder, seal = matches[0]
    manifest = read(folder / "dialogue.json")
    files = validate_projection(folder, manifest, seal)
    analysis = row.get("analysis_base")
    wave = analysis["wave_sha256"] if analysis else row["stock_wav_sha256"]
    expected_stock = None if analysis else bound["signed_stock"]
    require(seal.get("clock_format") == CLOCK_FORMAT
            and manifest["source_native_manifest_sha256"] == bound["native_manifest_sha256"]
            and manifest["locale"] == row["locale"]
            and manifest["source_wave_sha256"] == bound["source_wave_sha256"] == wave
            and manifest["original_spoken"] == expected_stock and bound["signed_stock"] == signed_binding(row)
            and row["reference_contract"] == {"sha256": bound["reference_contract_sha256"]}
            and row["request_reference_evidence"] == bound["ordered_reference_evidence"],
            "The admitted playback base and signed fetch plan have a different owner or reference")
    return state, folder, seal, files


def encode_choice(scratch, name_key, emitted, evidence, neighbor, fallback):
    stem = "expanded-name-" + digest(name_key.encode())[:24]
    clip = write_clip(scratch, stem, emitted)
    path = scratch / clip["file"]
    decoded, rate = sf.read(path, dtype="float32")
    conditioned = np.concatenate((decoded, neighbor))
    require(rate == RATE and len(decoded) == single_stream_end(path) == len(emitted)
            and len(decoded) == clip["samples"], "Expanded Name Ogg changed its measured delivery clock")
    before = abs(float(fallback[-1]) - float(neighbor[0]))
    after = abs(float(decoded[-1]) - float(neighbor[0]))
    require(after - before <= .004 and not join_discontinuities(conditioned, (len(decoded),)),
            "The decoded expanded Name join needs review. The existing fallback remains available.")
    clip.update(clock="decoded_ogg_pcm", unit=evidence["paced_unit_sha256"], cut=evidence["cut"])
    if "native_cut" in evidence:
        clip["native_cut"] = evidence["native_cut"]
    return clip, path.read_bytes()


def stage(app, row, bound, display_name, spoken_name, raw_audio, receipt, native_phones,
          request, provider, base_derived_audio, align, status, *, request_references=None):
    """Return privately prepared state, leaving the request and current installation intact."""
    require("analysis_source" not in bound, "Expanded analysis Name delivery awaits source review")
    require(isinstance(display_name, str) and len(display_name) <= 80 and NAME.fullmatch(display_name)
            and display_name.casefold() != "adventurer", "Choose a valid personalized display name")
    key = display_name.casefold()
    state, folder, seal, files = parent_route(app, row, bound)
    parent_hashes = {key: file_hash(folder / file) for key, file in PINS.items()}
    clock = read(folder / "clock-contract.json")
    metadata = read(folder / "native-selection-metadata.json")
    caption = read(folder / "caption.json")
    position = bound["delivered_part_index_1based"] - 1
    native_index = bound["native_marked_name_occurrence_1based"]
    require(position == 0 and len(clock["sequence"]) >= 2,
            "This expanded return adapter needs the admitted leading phrase")
    part, following = clock["sequence"][:2]
    owner = metadata["native_occurrences"][native_index - 1]
    require(part["kind"] == owner["kind"] == "name" and following["kind"] == "fixed"
            and part["native_occurrence_index_1based"] == native_index
            and len(part["native_sequence_indices_1based"]) > 1,
            "The exact leading Name scope is no longer present")
    previous = part["choices"].get(key)
    if previous:
        lineage = metadata.get(FIELD, {})
        require(lineage.get("display_name") == display_name and lineage.get("request_sha256") == request,
                "A different recording already owns this expanded Name")
        return state
    fallback = part["choices"][owner["fallback"]]
    require(fallback["sha256"] == bound["expanded_fallback_ogg_sha256"],
            "The existing expanded fallback changed")
    binding = next(item for item in caption["bindings"] if item["sequence_index_1based"] == position + 1)
    fixed_caption = binding["choices"][owner["fallback"]]
    a, b = bound["caption_name_chars"]
    require(binding["native_occurrence_index_1based"] == native_index
            and fixed_caption[a:b] == "Adventurer", "The caption Name span no longer matches the exact fallback")

    base_path = Path(base_derived_audio)
    require(file_hash(base_path) == bound["base_derived_wav_sha256"],
            "The whole paced source changed after expanded binding")
    base, rate = sf.read(base_path, dtype="float32")
    require(rate == RATE and base.ndim == 1 and len(base) == clock["sequence"][-1]["derived_span"][1],
            "The measured source clock changed before staging")
    work = app.folder / "spoken-player-names"
    pcm, evidence = prepare_named_cut(raw_audio, receipt, bound, row, spoken_name, request, provider,
                                      native_phones, work / "paced-units", align,
                                      request_references=request_references)
    emitted, pcm_hash = smooth_edges(pcm, base, part["derived_span"])
    with (folder / following["choices"]["fixed"]["file"]).open("rb") as stream:
        neighbor = sf.read(stream, dtype="float32")[0]
    with (folder / fallback["file"]).open("rb") as stream:
        prior = sf.read(stream, dtype="float32")[0]
    lineage = dict(format=FORMAT, display_name=display_name, native_index=native_index, delivered_index=position + 1,
                   caption_chars=[a,b], request_sha256=request, returned_wav_sha256=receipt["wav_sha256"],
                   reference_contract_sha256=bound["reference_contract_sha256"], **parent_hashes)
    lineage.update({field: evidence[field] for field in ("derived_phones_sha256", "paced_unit_sha256")})
    proof = dict(source_id=row["source_id"], npc_id=row["npc_id"], provider_id=provider, lineage=lineage,
                 prepared_cut=evidence, preencode_pcm_sha256=pcm_hash)
    target = work / "expanded-generated" / digest(canonical(proof))
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "delivery.zip"
    with hold(target, ".stage.lock"):
        path = target / "verification.json"
        if not archive.is_file():
            with TemporaryDirectory(dir=target) as temporary:
                clip, audio = encode_choice(Path(temporary), key, emitted, evidence, neighbor, prior)
            part["choices"][key] = clip
            owner["choice_keys"].append(key)
            owner["expected_values"].append(display_name)
            binding["choices"][key] = fixed_caption[:a] + display_name + fixed_caption[b:]
            metadata[FIELD] = lineage
            payload = {file: (folder / file).read_bytes() for file in sorted(files) if file.endswith(".ogg")}
            payload.update({clip["file"]: audio, "clock-contract.json": canonical(clock),
                            "native-selection-metadata.json": canonical(metadata), "caption.json": canonical(caption)})
            entry = {field: value for field, value in seal.items() if field != "clock_format"}
            entry.update(clock_contract_sha256=digest(payload["clock-contract.json"]),
                         native_selection_metadata_sha256=digest(payload["native-selection-metadata.json"]),
                         caption_sha256=digest(payload["caption.json"]))
            index = canonical(dict(format=BUNDLE, packages=[entry]))
            result = BytesIO()
            with ZipFile(result, "w", ZIP_STORED) as manifest:
                manifest.writestr("input.json", index)
                for name, content in payload.items():
                    manifest.writestr(entry["directory"] + "/" + name, content)
            content = result.getvalue()
            save(path, dict(proof=proof, clip=clip, input_sha256=digest(index), archive_sha256=digest(content)))
            cache_bytes(archive, content)
        recorded = read(path)
        require(recorded["proof"] == proof and recorded["archive_sha256"] == file_hash(archive),
                "The staged Name package changed. The full recording remains available for recovery.")
    return prepare_addition(app, dict(path=str(archive), sha256=recorded["archive_sha256"],
                                     input_sha256=recorded["input_sha256"]), status, base_state=state)
