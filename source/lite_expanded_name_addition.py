"""Validate a single additive Name upgrade against its admitted expanded base."""
from copy import deepcopy
import re

from lite_portable_io import file_hash, read

FIELD = "expanded_name_extension"
FORMAT = "dmspeaks-expanded-name-addition-1"
PINS = {
    "parent_clock_sha256": "clock-contract.json",
    "parent_metadata_sha256": "native-selection-metadata.json",
    "parent_caption_sha256": "caption.json",
}
HASH_FIELDS = frozenset(PINS) | {
    "returned_wav_sha256", "derived_phones_sha256", "request_sha256",
    "reference_contract_sha256", "paced_unit_sha256",
}
HASH = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"[^\W\d_]+(?:[ '-][^\W\d_]+)*")


def valid_lineage(value):
    fields = HASH_FIELDS | {
        "format", "native_index", "delivered_index", "caption_chars", "display_name",
    }
    if (not isinstance(value, dict) or value.keys() != fields or value["format"] != FORMAT
            or any(not isinstance(value[key], str) or not HASH.fullmatch(value[key]) for key in HASH_FIELDS)
            or any(type(value[key]) is not int or value[key] <= 0 for key in ("native_index", "delivered_index"))
            or not isinstance(value["display_name"], str) or len(value["display_name"]) > 80
            or not NAME.fullmatch(value["display_name"]) or value["display_name"].casefold() == "adventurer"):
        return False
    chars = value["caption_chars"]
    return (isinstance(chars, list) and len(chars) == 2 and all(type(x) is int for x in chars)
            and 0 <= chars[0] < chars[1] and chars[1] - chars[0] == len("Adventurer"))


def additive(previous, incoming):
    """Require exact parent media, source ownership, and the caption's original fixed prefix."""
    old_folder, old_manifest, _, _ = previous
    new_folder, new_manifest, _, _ = incoming
    lineage = new_manifest.get(FIELD)
    if not valid_lineage(lineage) or any(file_hash(old_folder / file) != lineage[key]
                                        for key, file in PINS.items()):
        return False
    old_clock, new_clock = (read(path / "clock-contract.json") for path in (old_folder, new_folder))
    old_metadata, new_metadata = (read(path / "native-selection-metadata.json")
                                  for path in (old_folder, new_folder))
    old_caption = deepcopy(old_manifest["reconstructed_caption"])
    new_caption = new_manifest["reconstructed_caption"]
    delivery = lineage["delivered_index"]
    original = lineage["native_index"]
    if not (delivery <= len(old_clock["sequence"]) and original <= len(old_metadata["native_occurrences"])):
        return False
    position = delivery - 1
    part = old_clock["sequence"][position]
    owner = old_metadata["native_occurrences"][original - 1]
    if (part["kind"] != "name" or owner["kind"] != "name"
            or part["native_occurrence_index_1based"] != original
            or len(part["native_sequence_indices_1based"]) <= 1):
        return False
    label = lineage["display_name"]
    key = label.casefold()
    if key in owner["choice_keys"] or position >= len(new_clock["sequence"]):
        return False
    clip = new_clock["sequence"][position]["choices"].get(key)
    if (not isinstance(clip, dict) or clip.get("unit") != lineage["paced_unit_sha256"]
            or any(clip.get("sha256") == existing["sha256"] or clip.get("file") == existing["file"]
                   for existing in part["choices"].values())):
        return False
    bindings = [entry for entry in old_caption["bindings"] if entry["sequence_index_1based"] == delivery]
    if len(bindings) != 1 or bindings[0]["native_occurrence_index_1based"] != original:
        return False
    values = bindings[0]["choices"]
    fallback = values[owner["fallback"]]
    start, stop = lineage["caption_chars"]
    spans = [match.span() for match in re.finditer(r"\bAdventurer\b", fallback)]
    if spans != [(start, stop)]:
        return False
    # Build the only allowed successor and require every other field to match.
    values[key] = fallback[:start] + label + fallback[stop:]
    owner["choice_keys"].append(key)
    owner["expected_values"].append(label)
    old_metadata[FIELD] = lineage
    part["choices"][key] = clip
    return old_clock == new_clock and old_metadata == new_metadata and old_caption == new_caption


def retains_installed_names(incoming, installed):
    """Recognize an older copy of the same base without downgrading active choices."""
    base_folder, base_manifest, base_seal, _ = incoming
    active_folder, active_manifest, active_seal, _ = installed
    lineage = active_manifest.get(FIELD)
    if (not valid_lineage(lineage)
            or base_manifest["delivery_clock_format"] != active_manifest["delivery_clock_format"]
            or any(base_seal[key] != active_seal[key] for key in (
                "source_native_manifest_sha256", "source_candidate_manifest_sha256"))):
        return False
    base_clock, active_clock = (read(folder / "clock-contract.json") for folder in (base_folder, active_folder))
    base_metadata, active_metadata = (read(folder / "native-selection-metadata.json")
                                     for folder in (base_folder, active_folder))
    base_caption = base_manifest["reconstructed_caption"]
    active_caption = deepcopy(active_manifest["reconstructed_caption"])
    delivered = lineage["delivered_index"]
    original = lineage["native_index"]
    if (delivered > min(len(base_clock["sequence"]), len(active_clock["sequence"]))
            or original > min(len(base_metadata["native_occurrences"]), len(active_metadata["native_occurrences"]))):
        return False
    base_part = base_clock["sequence"][delivered - 1]
    active_part = active_clock["sequence"][delivered - 1]
    owner = active_metadata["native_occurrences"][original - 1]
    if (base_part["kind"] != "name" or active_part["kind"] != "name" or owner["kind"] != "name"
            or active_part["native_occurrence_index_1based"] != original
            or len(active_part["native_sequence_indices_1based"]) <= 1):
        return False
    added = active_part["choices"].keys() - base_part["choices"].keys()
    if lineage["display_name"].casefold() not in added or owner["fallback"] in added:
        return False
    bindings = [entry for entry in active_caption["bindings"] if entry["sequence_index_1based"] == delivered]
    if len(bindings) != 1 or bindings[0]["native_occurrence_index_1based"] != original:
        return False
    values = bindings[0]["choices"]
    labels = {label.casefold(): label for label in owner["expected_values"]}
    fallback = values[owner["fallback"]]
    start, stop = lineage["caption_chars"]
    if [m.span() for m in re.finditer(r"\bAdventurer\b", fallback)] != [(start, stop)]:
        return False
    for key in added:
        label = labels.get(key)
        if not label or not NAME.fullmatch(label) or values.get(key) != fallback[:start] + label + fallback[stop:]:
            return False
        del active_part["choices"][key]
        del values[key]
    owner["choice_keys"] = [key for key in owner["choice_keys"] if key not in added]
    owner["expected_values"] = [label for label in owner["expected_values"] if label.casefold() not in added]
    base_metadata.pop(FIELD, None)
    active_metadata.pop(FIELD)
    return base_clock == active_clock and base_metadata == active_metadata and base_caption == active_caption
