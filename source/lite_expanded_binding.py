"""Load a path-free expanded binding and reconstruct its signed source clock."""
import json
import re
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from guide_bindings import RATE
from lite_expanded_return_cut import require
from lite_portable_io import digest, file_hash
from lite_portable_render import name_text
from lite_source_identity import signed_binding
from phoneme_alignment import word_offsets

FORMAT = "dmspeaks-expanded-signed-name-binding-1"
ANALYSIS_FORMAT = "dmspeaks-expanded-analysis-name-binding-1"
LOCATION = re.compile(r"expanded-names/([0-9a-f]{64})\.json")
HASH = re.compile(r"[a-f0-9]{64}")
ANALYSIS_HOLDS = {
    "voice-reference": "The expanded dialogue's voice reference is awaiting review.",
    "release-cut": "The expanded dialogue's final Name cut is awaiting listening review.",
}


def manifest_alias_member(checksum):
    return f"expanded-bases/{checksum}.json"


def check_analysis_binding(row, members, contract):
    from lite_portable_references import contract_member

    analysis = row.get("analysis_base")
    seed = row.get("companion_seed")
    evidence = contract["analysis_source"]
    require(isinstance(analysis, dict) and isinstance(seed, dict)
            and isinstance(evidence, dict) and set(evidence) == {
                "flac_sha256", "signed_seed_sha256", "review_hold"}
            and evidence["flac_sha256"] == analysis.get("sha256")
            and contract["source_wave_sha256"] == analysis.get("wave_sha256")
            and evidence["signed_seed_sha256"] == seed.get("manifest_sha256")
            and isinstance(evidence["review_hold"], str) and evidence["review_hold"] in ANALYSIS_HOLDS,
            "The expanded analysis binding needs its sealed recording, signed seed and review hold")
    alias = manifest_alias_member(contract["native_manifest_sha256"])
    bundled = f"companions/{seed['manifest_sha256']}/dialogue.json"
    require(alias in members and bundled in members, "The expanded analysis native manifest pair is missing")
    original, sealed = members[alias], members[bundled]
    require(len(original) <= 1_000_000 and len(sealed) <= 1_000_000
            and digest(original) == contract["native_manifest_sha256"]
            and digest(sealed) == seed["manifest_sha256"],
            "The expanded analysis native manifest pair changed")
    native = json.loads(original)
    require(isinstance(native, dict)
            and "original_spoken" in native and native["original_spoken"] is None
            and not native.keys() & {"delivery_variant", "reference_spoken"}
            and native.get("source_wave_sha256") == contract["source_wave_sha256"]
            and native.get("source_id") == row["source_id"] and native.get("npc_id") == row["npc_id"]
            and native.get("source_samples") == row["samples"] and native.get("locale") == row["locale"]
            and json.loads(sealed) == dict(native, delivery_variant=analysis.get("seed_variant"),
                                          reference_spoken=signed_binding(row)),
            "The signed analysis seed changes the native dialogue or its clip recipe")
    reference_hash = contract["reference_contract_sha256"]
    reference_path = contract_member(reference_hash)
    require(reference_path in members, "The expanded analysis voice reference is missing")
    reference = members[reference_path]
    require(len(reference) <= 2_000_000 and digest(reference) == reference_hash
            and json.loads(reference).get("mode") == "original",
            "The expanded analysis needs the exact original voice assembly")


def descriptor(row, members):
    name = row.get("expanded_name_binding")
    if name is None:
        return None
    match = LOCATION.fullmatch(name) if isinstance(name, str) else None
    require(match is not None and name in members, "The expanded Name binding is missing")
    raw = members[name]
    require(len(raw) <= 20000 and digest(raw) == match[1], "The expanded Name binding changed")
    contract = json.loads(raw)
    fields = {
        "format", "source_id", "npc_id", "signed_stock", "source_wave_sha256",
        "native_manifest_sha256", "candidate_manifest_sha256", "reference_contract_sha256",
        "expanded_fallback_ogg_sha256", "delivered_part_index_1based", "native_marked_name_occurrence_1based",
        "caption_name_chars", "fixed_prefix_words", "fallback_caption",
        "base_derived_pcm_sha256", "base_derived_samples",
    }
    is_analysis = isinstance(contract, dict) and contract.get("format") == ANALYSIS_FORMAT
    require(isinstance(contract, dict)
            and contract.keys() == (fields | {"analysis_source"} if is_analysis else fields)
            and contract["format"] in (FORMAT, ANALYSIS_FORMAT),
            "The expanded Name binding has an unsupported format")
    require(not any(field in row for field in ("native_source", "name_slots"))
            and is_analysis == ("analysis_base" in row)
            and contract["source_id"] == row["source_id"] and contract["npc_id"] == row["npc_id"]
            and contract["signed_stock"] == signed_binding(row)
            and row.get("reference_contract") == {"sha256": contract["reference_contract_sha256"]},
            "The binding belongs to another signed dialogue, source clock or voice reference")
    require(all(isinstance(value, str) and HASH.fullmatch(value)
                for key, value in contract.items() if key.endswith("_sha256")),
            "The expanded Name binding lacks measured identities")
    if is_analysis:
        check_analysis_binding(row, members, contract)
    else:
        seed = row.get("companion_seed")
        require(contract["source_wave_sha256"] == row["stock_wav_sha256"]
                and (seed is None or seed.get("manifest_sha256") == contract["native_manifest_sha256"]),
                "The expanded Name binding does not match the signed plan's native companion seed or source clock")
    require(type(contract["delivered_part_index_1based"]) is int and contract["delivered_part_index_1based"] == 1
            and type(contract["native_marked_name_occurrence_1based"]) is int
            and contract["native_marked_name_occurrence_1based"] > 1
            and type(contract["base_derived_samples"]) is int and 0 < contract["base_derived_samples"] <= RATE * 120,
            "The expanded Name binding needs an admitted leading context and sample clock")
    fallback, span = name_text(row, "Adventurer")
    prefix = contract["fixed_prefix_words"]
    require(isinstance(prefix, list) and prefix and all(isinstance(word,str) and word for word in prefix)
            and fallback == contract["fallback_caption"] and list(span) == contract["caption_name_chars"]
            and list(word_offsets(fallback, [*prefix, "Adventurer"]))[-1] == span,
            "The expanded Name binding changes the context or marked lexical span")
    return dict(contract, ordered_reference_evidence=row["request_reference_evidence"])


def check_pcm(path, contract):
    pcm, rate = sf.read(path, dtype="float32")
    require(rate == RATE and pcm.ndim == 1 and len(pcm) == contract["base_derived_samples"]
            and digest(np.ascontiguousarray(pcm, dtype="<f4").tobytes()) == contract["base_derived_pcm_sha256"],
            "The locally reconstructed expanded source differs from its admitted sample clock")


def local_base(scratch, row, members, stock, contract):
    """Generate the measured paced source from the verified catalog or installed Ogg."""
    from lite_portable_inputs import analysis_pcm, decode_stock, render_inputs, stock_decode_contract, wave_bytes

    scratch = Path(scratch)
    original, base = scratch / "unpaced-source.wav", scratch / "paced-source.wav"
    if contract["format"] == ANALYSIS_FORMAT:
        require(file_hash(Path(stock)) == contract["signed_stock"]["sha256"],
                "The installed signed dialogue changed before analysis reconstruction")
        decode_stock(stock, stock_decode_contract(row), scratch / "signed-reference.wav")
        original.write_bytes(wave_bytes(analysis_pcm(row, members)))
    else:
        render_inputs(stock, row, original, members)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(original),
                    "-af", "atempo=16/15", "-ar", str(RATE), "-ac", "1",
                    "-n", "-c:a", "pcm_f32le", str(base)], check=True, capture_output=True,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    check_pcm(base, contract)
    return base, dict(contract, base_derived_wav_sha256=file_hash(base))
