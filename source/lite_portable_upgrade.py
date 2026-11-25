"""Carry reusable player-name clips forward when the prepared catalog changes."""

from copy import deepcopy
import re

from guide_bindings import RATE
from lite_portable_constraints import name_occurrences
from lite_portable_inputs import INPUTS, unpack_plans
from lite_portable_io import cache_bytes, digest, file_hash, read, save, stock_binding, validate_routes


COVERAGE_FIELDS = {"area", "races", "classes", "sexes"}
REFERENCE_STORAGE_FIELDS = {"reference_from_stock"}
PORTABLE_ANALYSIS_FIELDS = {"stock_pcm_repair", "paced_base_analysis"}


def dialogue_folder(packages, row):
    from lite_source_identity import source_directory, source_key
    root='DMSpeaksSpokenNames'
    if row.get('native_source'):root+='-native-'+row['native_source']['source_contract_sha256'][:20]
    if row.get("quest_variant"):
        root += "-quest-variant-" + source_key(row).partition(":")[2][:20]
    return packages / root / "generated/sounds" / source_directory(row) / row["key"]


def same_recipe(before, after):
    from lite_source_identity import native_identity
    def recipe(row):
        value={key:item for key,item in row.items()
               if key not in COVERAGE_FIELDS | REFERENCE_STORAGE_FIELDS | PORTABLE_ANALYSIS_FIELDS | {'native_prefetch'}}
        if row.get('native_source'):value['native_source']=native_identity(row)
        return value
    return recipe(before)==recipe(after)


def matches_prepared_dialogue(manifest, row):
    """Check the source and recording behind a staged dialogue manifest."""
    from lite_source_identity import native_identity, signed_binding

    return (stock_binding(manifest) == signed_binding(row)
            and manifest.get('native_source') == native_identity(row)
            and manifest.get("quest_variant") == row.get("quest_variant")
            and manifest.get("analysis_base_sha256") == row.get("analysis_base", {}).get("sha256")
            and manifest.get("source_pcm_sha256") == row["pcm_sha256"]
            and manifest.get("companion_seed") == row.get("companion_seed")
            and manifest.get("reference_contract") == row.get("reference_contract")
            and manifest.get("source_id") == row["source_id"]
            and manifest.get("npc_id") == row["npc_id"]
            and manifest.get("locale") == row["locale"]
            and manifest.get("sample_rate") == RATE
            and manifest.get("source_samples") == row["samples"]
            and manifest.get("spoken_key") == row["key"])


def carry_dialogue(source, target, row, *, admitted_manifest=None):
    previous = target / "dialogue.json"
    source_manifest = source / "dialogue.json"
    if previous.is_symlink() or source_manifest.is_symlink():
        return
    manifest = read(source_manifest) if admitted_manifest is None else deepcopy(admitted_manifest)
    if not matches_prepared_dialogue(manifest, row):
        return
    old_names = [part for part in manifest["sequence"] if part["kind"] == "name"]
    slots = name_occurrences(row)
    if [(part["occurrence"], part["source_samples"]) for part in old_names] != [
            (number, slot["source_cut"]) for number, slot in enumerate(slots, 1)]:
        return
    source_files = validate_routes(source, manifest, source)
    if previous.is_file():
        current = read(previous)
        static = lambda data: [part for part in data["sequence"] if part["kind"] != "name"]
        same_source = ("original_spoken", "reference_spoken", "analysis_base_sha256", "reconstructed_caption",
                       "source_pcm_sha256", "companion_seed", "source_id", "npc_id", "source_samples",
                       "spoken_key", "sample_rate", "locale", "reference_contract",'native_source')
        if (any(current.get(key) != manifest.get(key) for key in same_source)
                or static(current) != static(manifest)
                or current.get("character_sequences") or manifest.get("character_sequences")):
            return
        new_names = [part for part in current["sequence"] if part["kind"] == "name"]

        def boundaries(parts):
            return [(part["occurrence"], part["source_samples"], part["fallback"], part["choices"][part["fallback"]])
                    for part in parts]

        if boundaries(new_names) != boundaries(old_names):
            return
        reserved = validate_routes(target, current, target)
        transferable = [
            {name for name, clip in old["choices"].items()
             if "measured_name" in clip and (name in new["choices"] or clip["file"] not in reserved)}
            for old, new in zip(old_names, new_names, strict=True)
        ]
        complete = set.intersection(*transferable)
        source_files = set()
        for old, new in zip(old_names, new_names, strict=True):
            added = {name: clip for name, clip in old["choices"].items() if name in complete and name not in new["choices"]}
            new["choices"].update(deepcopy(added))
            new["expected_values"] += [value for value in old["expected_values"] if value.casefold() in added]
            source_files.update(clip["file"] for clip in added.values())
        if not source_files:
            return
        manifest = current
    for name in sorted(source_files):
        cache_bytes(target / name, (source / name).read_bytes())
    if row.get('native_source'):
        from lite_source_identity import native_filters
        manifest['native_character_filters']=native_filters(row)
    validate_routes(target, manifest, target)
    save(previous, manifest)


def admit_legacy_references(app, before, row, folder, members):
    """Upgrade only cached names whose exact earlier paired request is reproduced."""
    from lite_name_providers import NameProvider
    from lite_portable_references import reference_evidence, verified_references
    from lite_portable_render import expanded, name_text
    from phoneme_alignment import transcript_digest

    if (before.get('native_source') or row.get('native_source')
            or members is None or "reference_contract" in before or "reference_contract" not in row
            or not before.get("archived_request_references")
            or before["archived_request_references"] != row.get("request_reference_evidence")):
        return None
    new_fields = {"reference_contract", "request_reference_evidence", "source_text_sha256"}
    if ({key: value for key, value in before.items() if key not in COVERAGE_FIELDS | new_fields}
            != {key: value for key, value in row.items() if key not in COVERAGE_FIELDS | new_fields}):
        return None
    references, _ = verified_references(row, members)
    expected = reference_evidence(references)
    if expected != before["archived_request_references"]:
        return None
    manifest = read(folder / "dialogue.json")
    if manifest.get("reference_contract") is not None:
        return None
    validate_routes(folder, manifest, folder)
    names = [part for part in manifest["sequence"] if part["kind"] == "name"]
    slots = name_occurrences(row)
    if len(names) != len(slots):
        return None
    admitted = []
    for part, slot in zip(names, slots, strict=True):
        kept = set()
        for value, clip in part["choices"].items():
            if "measured_name" not in clip:
                continue
            spelling = next((text for text in part["expected_values"] if text.casefold() == value), None)
            if spelling is None:
                continue
            transcript, _ = name_text(slot, spelling)
            if clip["measured_name"]["transcript_sha256"] != transcript_digest(expanded(transcript)):
                continue
            provider = NameProvider(app.connections, clip["provider_id"], submit=False,
                                    allow_metered=False, timestamps=True)
            wire = provider.request(transcript, row["npc_id"], references)
            if wire.get("reference_evidence") != expected or digest(wire["body"]) != clip["request_sha256"]:
                continue
            kept.add(value)
        admitted.append(kept)
    complete = set.intersection(*admitted)
    if not complete:
        return None
    for part in names:
        part["choices"] = {value: clip for value, clip in part["choices"].items()
                           if value == part["fallback"] or value in complete}
        part["expected_values"] = [text for text in part["expected_values"]
                                   if text.casefold() == part["fallback"] or text.casefold() in complete]
        for value in complete:
            part["choices"][value]["reference_contract_sha256"] = row["reference_contract"]["sha256"]
    manifest.update(reference_contract=row["reference_contract"], source_text_sha256=row["source_text_sha256"])
    return manifest


def carry_compatible(app, packages, rows, *, members=None):
    """Reuse exact recipes from old catalogs before sending a provider request."""
    from lite_source_identity import source_key
    by_stock = {source_key(row): row for row in rows}
    catalogs = app.folder / "lite-inputs"
    for old in sorted(packages.parent.iterdir()) if packages.parent.is_dir() else ():
        if old == packages or old.is_symlink() or not old.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", old.name):
            continue
        archive = catalogs / f"plans-{old.name}.zip"
        try:
            if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > INPUTS["plans"][1]:
                continue
            if file_hash(archive) != old.name:
                continue
            plan, _ = unpack_plans(archive)
        except (OSError, ValueError):
            continue
        for before in plan["dialogues"]:
            row = by_stock.get(source_key(before))
            if not row:
                continue
            source = dialogue_folder(old, before)
            target = dialogue_folder(packages, row)
            if (not (source / "dialogue.json").is_file()
                    or source.resolve() != source or target.resolve() != target):
                continue
            try:
                admitted = None
                if not same_recipe(before, row):
                    admitted = admit_legacy_references(app, before, row, source, members)
                    if admitted is None:
                        continue
                carry_dialogue(source, target, row, admitted_manifest=admitted)
            except (OSError, KeyError, TypeError, ValueError) as error:
                app.activity.append("warning", f"An older name cache could not be carried forward: {error}",
                                    {"operation": "spoken_player_names", "source_id": row["source_id"]})
