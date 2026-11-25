"""Reuse verified fixed clips and non-name choices from an installed companion."""

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re

from guide_bindings import RATE
from lite_portable_constraints import STARTING_ROUTES, companion_leading_word, name_occurrences
from lite_races import spoken_race
from lite_portable_io import archive_names_under, cache_bytes, digest, validate_routes
from lite_seed_joins import SeedJoins, read_seed_joins, sequence_bindings


@dataclass(frozen=True)
class Companion:
    folder: Path | None
    sequence: list
    files: set
    joins: SeedJoins | None = None


def fallback_context_articles(sequence, row, kind):
    """Find article corrections carried by a verified fallback in this request window."""
    lo, hi = row["context"]
    articles = []
    for part in sequence:
        if part["kind"] != kind:
            continue
        leading = companion_leading_word(part["choices"][part["fallback"]].get("leading_word"), row)
        if not leading or {leading["source_text"].casefold(), leading["spoken_text"].casefold()} != {"a", "an"}:
            continue
        start, end = leading["source_chars"]
        if lo < end and start < hi:
            if not lo <= start < end <= hi:
                raise ValueError("The name context starts or ends inside a corrected article.")
            articles.append({"source_chars": [start, end], "spoken_text": leading["spoken_text"],
                             "occurrence": part["occurrence"]})
    return sorted(articles, key=lambda article: article["source_chars"])


def class_context_articles(sequence, row):
    return fallback_context_articles(sequence, row, "class")


def companion_folder(addons, row):
    seed = row["companion_seed"]
    if row.get('native_source'):
        from lite_source_identity import source_directory
        relative=Path('generated/sounds')/source_directory(row)/row['key']
    else:
        relative = Path(row["stock_path"]).relative_to(Path(row["stock_path"]).parts[0]).with_suffix("")
    if "analysis_base" in row:
        # Keep the currently installed playback variant available to its loader.
        relative = relative.with_name(relative.name + "-seed-" + seed["manifest_sha256"][:20])
    return addons.resolve() / seed["addon"] / relative


def bundled_files(row, members):
    seed = row["companion_seed"]
    if row.get("native_source"):
        from lite_native_sources import bundled_seed
        bundled = bundled_seed(row, members)
    else:
        prefix = "companions/" + seed["manifest_sha256"] + "/"
        bundled = {name.removeprefix(prefix): members[name]
                   for name in archive_names_under(members, prefix)}
    if not bundled:
        return bundled
    if "dialogue.json" not in bundled or len(bundled["dialogue.json"]) > 1_000_000:
        raise ValueError("The prepared catalog has an incomplete playback seed.")
    if digest(bundled["dialogue.json"]) != seed["manifest_sha256"]:
        raise ValueError("The prepared playback seed differs from its dialogue binding.")
    if any(Path(name).name != name or name in {"", ".", ".."} for name in bundled):
        raise ValueError("The prepared playback seed contains an invalid clip path.")
    return bundled


def validate_bundled_companion(row, members):
    if compact_companion(row, members) is not None:
        return
    bundled = bundled_files(row, members)
    if not bundled:
        raise ValueError("The prepared catalog has an incomplete playback seed.")
    verify_bundled_companion(row, bundled)


def verify_bundled_companion(row, bundled):
    _, files = verified_companion(row, bundled["dialogue.json"], None, clip_bytes=bundled)
    if set(bundled) != files | {"dialogue.json"}:
        raise ValueError("The prepared playback seed includes unrelated files.")


def compact_companion(row, members):
    seed = row.get("companion_seed")
    if not seed or members is None or row.get("native_source"):
        return None
    checksum = getattr(members, "companion_joins", {}).get(seed["manifest_sha256"])
    if checksum is None:
        return None
    prefix = "companions/" + seed["manifest_sha256"] + "/"
    manifest, joins = prefix + "dialogue.json", prefix + "joins.json"
    if set(archive_names_under(members, prefix)) != {manifest, joins}:
        raise ValueError("The measured playback seed has an unexpected file layout.")
    raw = members[manifest]
    if len(raw) > 1_000_000 or digest(raw) != seed["manifest_sha256"]:
        raise ValueError("The measured playback seed differs from its dialogue binding.")
    sequence, files = verified_companion(row, raw, None, manifest_only=True)
    return Companion(None, sequence, files, read_seed_joins(members[joins], checksum, sequence))


def installed_companion(addons, row, *, members=None, cache=None, verify_only=False, store_managed=False):
    seed = row.get("companion_seed")
    if seed is None:
        return None
    if compact := compact_companion(row, members):
        if not store_managed:
            raise ValueError("The measured Name joins require the installed DMSpeaks shared playback.")
        return compact
    folder = companion_folder(addons, row)
    bundled = bundled_files(row, members) if members is not None else {}
    if bundled:
        if verify_only:
            verify_bundled_companion(row, bundled)
            return None
        if cache is None:
            raise ValueError("The prepared catalog has an incomplete playback seed.")
        cache_root = Path(cache) / seed["manifest_sha256"]
        if cache_root.resolve() != cache_root:
            raise ValueError("The prepared playback cache leaves its selected folder.")
        folder = companion_folder(cache_root, row)
        if folder.resolve() != folder:
            raise ValueError("The prepared playback cache leaves its selected folder.")
        for name, data in bundled.items():
            cache_bytes(folder / name, data)
    path = folder / "dialogue.json"
    if path.resolve() != path or not path.is_file() or path.stat().st_size > 1_000_000:
        raise ValueError("Install the verified DMSpeaks companion for this mixed dialogue.")
    raw = path.read_bytes()
    if digest(raw) != seed["manifest_sha256"]:
        raise ValueError("The installed DMSpeaks mixed dialogue differs from the prepared version.")
    sequence, files = verified_companion(row, raw, folder)
    if bundled and set(bundled) != files | {"dialogue.json"}:
        raise ValueError("The prepared playback seed includes unrelated files.")
    return Companion(folder, sequence, files)


def verified_companion(row, raw, folder, *, clip_bytes=None, manifest_only=False):
    analysis = row.get("analysis_base")
    manifest = json.loads(raw)
    from lite_source_identity import signed_binding
    binding = {"original_spoken": signed_binding(row),
               "source_id": row["source_id"], "spoken_key": row["key"], "npc_id": row["npc_id"],
               "locale": row["locale"], "sample_rate": RATE, "source_samples": row["samples"],
               "source_wave_sha256": row['source_wave_sha256'] if row.get('native_source') else row["stock_wav_sha256"]}
    if analysis:
        if not row.get("requires_paced_base"):
            binding.update(reference_spoken=binding["original_spoken"], delivery_variant=analysis["seed_variant"])
        binding.update(original_spoken=None, source_wave_sha256=analysis["wave_sha256"],
                       original_text=analysis["quest_text"])
    if any(manifest.get(key) != value for key, value in binding.items()):
        raise ValueError("The installed DMSpeaks companion belongs to another dialogue or sample clock.")

    sequence = manifest["sequence"]
    names = [part for part in sequence if part["kind"] == "name"]
    other = [part for part in sequence if part["kind"] not in {"fixed", "name"}]
    slots = name_occurrences(row)
    requires_paced = row.get("requires_paced_base") is True
    measured_base = row.get("paced_base_analysis")
    if (len(names) != len(slots) or (requires_paced and other and not measured_base)
            or (len(slots) == 1 and not other and not analysis and not row.get('native_source') and not requires_paced)
            or any(part["kind"] not in {"class", "gender", "race"} for part in other)):
        raise ValueError("The DMSpeaks companion needs the prepared Name slots and supported choices.")
    for occurrence, (name, slot) in enumerate(zip(names, slots, strict=True), 1):
        if (name["occurrence"] != occurrence or name["source_samples"] != slot["source_cut"]
                or name["fallback"] != "adventurer"):
            raise ValueError("The DMSpeaks companion Name slots differ from the prepared clock.")
        fallback = name["choices"]["adventurer"]
        if companion_leading_word(fallback.get("leading_word"), row) != slot.get("leading_word"):
            raise ValueError("The DMSpeaks Name boundary differs from its measured leading word.")
        if (suffix := slot.get("name_suffix")) and not re.search(
                re.escape(name["delimiter"] + suffix) + r"(?!\w)", manifest["original_text"]):
            raise ValueError("The companion does not bind this possessive suffix to the Name delimiter.")
        name["choices"] = {"adventurer": fallback}
        name["expected_values"] = ["Adventurer"]
    for part in other:
        if part["kind"] == "class" and part["fallback"] != "adventurer":
            raise ValueError("A DMSpeaks class occurrence needs its contextual Adventurer fallback.")
        if part["kind"] == "race":
            if (part["delimiter"].casefold() != "$r" or part["fallback"] != "adventurer"
                    or not set(part["choices"]) <= {"adventurer", *(spoken_race(race) for race in STARTING_ROUTES)}):
                raise ValueError("A DMSpeaks race occurrence needs its contextual Adventurer fallback and supported races.")
        if part["kind"] == "gender":
            branches = re.fullmatch(r"\$[gG]([^:;]+):([^;]+);", part["delimiter"])
            values = part["value_by_player_sex"]
            if (not branches or values != dict(zip(("2", "3"), (text.strip().casefold() for text in branches.groups())))
                    or set(values.values()) != set(part["choices"]) or part["fallback"] not in values.values()):
                raise ValueError("The DMSpeaks gender choices must follow both source branches.")
    for slot in slots:
        for kind in ("class", "race"):
            if fallback_context_articles(sequence, slot, kind) != slot.get(kind + "_articles", []):
                raise ValueError(f"The request article differs from the verified DMSpeaks {kind} fallback.")
    manifest.pop("character_sequences", None)
    try:
        files = validate_routes(folder, manifest, folder, clip_bytes=clip_bytes,
                                sealed_clips=sequence_bindings(sequence) if manifest_only else None)
    except FileNotFoundError as error:
        raise ValueError("The playback seed is missing a required clip.") from error
    return sequence, files


def seed_clips(destination, companion, manifest):
    manifest["sequence"] = deepcopy(companion.sequence)
    if companion.folder is not None:
        for filename in companion.files:
            cache_bytes(destination / filename, (companion.folder / filename).read_bytes())
