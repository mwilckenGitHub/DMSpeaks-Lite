"""Validate expanded ownership before projecting the delivered playback route."""
from copy import deepcopy
import re

FORMAT = "dmspeaks-paced-companion-expanded-clock-2"
SELECTION = "dmspeaks-paced-companion-expanded-selection-2"
BUNDLE = "dmspeaks-paced-companion-expanded-deliveries-2"


def positive_span(span, cursor):
    return (isinstance(span, list) and len(span) == 2
            and all(type(value) is int for value in span)
            and span[0] == cursor and span[1] > cursor)


def project(clock, metadata, trust, caption):
    from lite_paced_delivery import FORMAT as BASE_FORMAT, KINDS, projection, require

    require(clock.get("format") == FORMAT and metadata.get("format") == SELECTION,
            "Unsupported expanded delivery or selection format.")
    require(metadata.get("native_manifest_sha256") == trust["source_native_manifest_sha256"],
            "The expanded selection belongs to a different native source.")
    require(isinstance(caption, dict), "An expanded delivery needs its sealed source caption.")
    native = metadata["native_occurrences"]
    owners = metadata["delivered_owners"]
    parts = clock["sequence"]
    require(isinstance(native, list) and native and type(clock["native_part_count"]) is int
            and clock["native_part_count"] == len(native), "An original native occurrence is missing.")
    require(isinstance(owners, list) and isinstance(parts, list) and parts and len(owners) == len(parts),
            "An expanded delivered owner is missing.")

    cursor = 0
    occurrences = set()
    for index, part in enumerate(native, 1):
        span = part["source_samples"]
        kind = part["kind"]
        require(type(part["sequence_index_1based"]) is int and part["sequence_index_1based"] == index
                and kind in KINDS and positive_span(span, cursor),
                "Original native parts must partition the source in order.")
        cursor = span[1]
        if kind == "fixed":
            require(part["choice_keys"] == [] and not any(
                key in part for key in ("occurrence", "delimiter", "fallback", "expected_values", "value_by_player_sex")),
                "Only unselectable fixed context can be absorbed.")
            continue
        occurrence = part["occurrence"]
        delimiter = part["delimiter"]
        require(type(occurrence) is int and occurrence > 0 and (kind, occurrence) not in occurrences,
                "A native delimiter occurrence was duplicated or changed.")
        occurrences.add((kind, occurrence))
        pattern = {"name": r"\$n", "race": r"\$r", "class": r"\$c", "gender": r"\$g[^:;]+:[^;]+;"}[kind]
        require(isinstance(delimiter, str) and re.fullmatch(pattern, delimiter, re.IGNORECASE),
                "A native delimiter does not match its kind.")
        keys = part["choice_keys"]
        require(isinstance(keys, list) and keys and all(isinstance(key, str) and key for key in keys)
                and len(set(keys)) == len(keys) and part["fallback"] in keys,
                "A native delimiter has missing or repeated choices.")
    require(cursor == metadata["source_samples"], "The native route does not cover the complete source.")

    next_native = 1
    expanded = set()
    delivered_occurrences = []
    for delivered, (part, owner) in enumerate(zip(parts, owners, strict=True), 1):
        indices = part["native_sequence_indices_1based"]
        require(isinstance(indices, list) and indices and all(type(index) is int for index in indices)
                and indices == list(range(next_native, next_native + len(indices)))
                and indices[-1] <= len(native),
                "Native ownership is missing, reordered, foreign or duplicated.")
        occurrence_index = part["native_occurrence_index_1based"]
        occurrence = native[indices[-1] - 1]
        span = [native[indices[0] - 1]["source_samples"][0], occurrence["source_samples"][1]]
        require(type(occurrence_index) is int and occurrence_index == indices[-1]
                and part["kind"] == occurrence["kind"] and part["native_span"] == span,
                "An expanded delivered clip changed its native occurrence or span.")
        require(type(owner["sequence_index_1based"]) is int
                and type(owner["native_occurrence_index_1based"]) is int
                and isinstance(owner["native_sequence_indices_1based"], list)
                and all(type(index) is int for index in owner["native_sequence_indices_1based"])
                and owner == dict(sequence_index_1based=delivered, native_span=span, kind=part["kind"],
                                  native_occurrence_index_1based=occurrence_index,
                                  native_sequence_indices_1based=indices),
                "The expanded clock and selection ownership maps disagree.")
        if len(indices) > 1:
            require(part["kind"] != "fixed"
                    and all(native[index - 1]["kind"] == "fixed" for index in indices[:-1]),
                    "Only preceding fixed parts may join one delimiter occurrence.")
            expanded.add(delivered)
        next_native = indices[-1] + 1
        delivered_occurrences.append(dict(occurrence, sequence_index_1based=delivered, source_samples=span))
    require(next_native == len(native) + 1 and expanded,
            "The expanded route must own every native part and include a fixed prefix.")

    for binding in caption.get("bindings", []):
        delivered = binding["sequence_index_1based"]
        native_index = binding["native_occurrence_index_1based"]
        require(type(delivered) is int and 1 <= delivered <= len(parts) and type(native_index) is int
                and native_index == parts[delivered - 1]["native_occurrence_index_1based"],
                "An expanded caption has a different native occurrence owner.")
        if delivered in expanded:
            first, last = binding["first"], binding["last"]
            starts = caption["word_samples"]
            a, b = parts[delivered - 1]["native_span"]
            delimiter_start = native[native_index - 1]["source_samples"][0]
            require(type(first) is int and type(last) is int and 1 <= first <= last <= len(starts)
                    and a <= starts[first - 1] < delimiter_start <= starts[last - 1] < b,
                    "An expanded caption must bind its fixed prefix and its delimiter.")

    # Reuse the unchanged v1 playback, choice, event and caption validation
    # only after this format's exhaustive native ownership checks pass.
    result = projection(dict(clock, format=BASE_FORMAT),
                        dict(metadata, occurrences=delivered_occurrences), trust, caption)
    result["delivery_clock_format"] = FORMAT
    result["native_occurrences"] = deepcopy(native)
    result["delivered_owners"] = deepcopy(owners)
    from lite_expanded_name_addition import FIELD, valid_lineage
    if FIELD in metadata:
        lineage = metadata[FIELD]
        require(valid_lineage(lineage), "The expanded Name addition lacks bound lineage.")
        choice = result["sequence"][lineage["delivered_index"] - 1]["choices"][lineage["display_name"].casefold()]
        if "cut" in choice:
            require(choice["cut"]["returned_sha256"] == lineage["returned_wav_sha256"],
                    "The expanded Name cut belongs to a different returned recording.")
        result[FIELD] = deepcopy(lineage)
    return result
