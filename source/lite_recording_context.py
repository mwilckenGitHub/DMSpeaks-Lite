"""Render one selected name in its installed English WoW dialogue, without contacting a provider."""
from hashlib import sha256
from io import BytesIO
import json
import re
from pathlib import Path, PurePosixPath

from lite_context_routes import (
    ContextUnavailable, entry_for, original_choices, profile_choices, read_table, require, selected_pieces,
)

RATE = 44100
MAX_FRAMES = 300 * RATE
PACK = Path("DMSpeaksSpokenNames")
# The verified release contains one empty name-alias map, no legacy named redirects,
# and identical entries wherever a revision is registered under several WoW triggers.
# A different playback program or data layout requires checking the same contract.
GAME_FILES = {
    "Playback.lua": ("0930bd228033d0fc61b2324f099345ae88dd3fb1e7703cc37f93d5a76e4f3ec7",
                     "cdd6add3f739d803ccab1d344f4f1ae82cd81c220c612a6087710ed646fb338a",
                     "11657fa245ff7a369cc5aeb615c292cdd30cc789eeeb56e7605f2df5082748bf"),
    "DialogueIndex.lua": "55bbf9f51db766209dcba47631fcff485b7baf31a689bab29b9bd280b363563f",
    "OriginalCuts.lua": "49756933cc0e47703b81b2d6146ff022c390a871cd56e55534802ad17130f5c0",
    "ManagedPlayback.json": "3597af3bbb4b3894bbb7009e36e9867b39f7cf3fede215b5983051b220a73903",
    "PlaybackRepairs.json": "efe370fc794bb75edaabab3da407f219754dfb313f5cbe3d5e348e56ece549cc",
}
UPDATED_PLAYBACK = "762f1e9894f7645884f920196939df338f5ad3457e587db82dff1e426eaae068"
TYPED_FALLBACK_PLAYBACK = "8ef80c00479da9c74ce5aca9eaf5695ab439789ebac40d562e6dc3d6cc2ed0d6"
SKYBORNE_PLAYBACK = "5ae296ee4c72a0cbdeb096bf43c155b8ab8344bffdfa6ccc6245fcee3c151082"
QUEST_REPLAY_PLAYBACK = "7f0e650cf9f06f4eafe6e829f256e2df3ff0022d92e766b18eb077049b0a9415"
CHATTER_PLAYBACK = "f80cbadc736d12f01202e86aee78b250de04d21a0af6b41913eaeff9efcc9a02"
ITEM_GUID_PLAYBACK = "a0fc9fe624b5fdb036e844d22cde16f364f7abbf831973051b191caa4e860749"
INVENTORY_INSTANCE_PLAYBACK = "60fdda929dc2c6eb30808e6b126c754f098e79c434f7429bfbe1dd313ec25715"
EDITION_TEXT_PLAYBACK = "94f4c318a7c230c47309114054e0e3969b18d608196a08182de9d997c74b4799"
# The current names catalog and its conditional Lua registrations are shipped together by Setup.
REVIEWED_JOIN_PLAYBACK = "d816371b96893f2dd727b601840a32859dcdecda06baea9c80e208c992a3ffe2"
CONTEXT_PROJECTION_SHA256 = "65d9162111b64be2df1952605899f1aa72edd62437277cfe18668c72d97cdb77"
STOCK_REGIONS = ("Alliance", "Horde", "Shared", "Gossip")

# Each additional release pairs its managed store with recipes captured from that build's WoW registrations.
REVIEWED_CATALOGS = {
    "861f29e5ba73efe6eb938ecdba91c1f806e6dabf3c03db1c69649a9a7e430840":
        "1a995f067d1f9cdfa751368df227559fb81d946e6365d47b0f1f10edadcbaf04",
    "32e1925e341422c7a449fb6e2e14c9c978ad4d53c573b830f275d59990ceb549":
        ("d69c110de6ff028be479efd1301a3d40110d94ab9d80010f76d44a40efca5cee",
         "07fe3dca7ec909e20df8680a697c6e029ebea9b34e6c0cbcfb505029324429fb",
         "3d2e51517faa3c1bf6da35b5db1bb840ad9183dea8eb460e19818c9c2ccf062f"),
    "9234b0952397b98d9edbde2117c6799d6bdfd84f252695b5fa845b79c36c938a":
        "e6549faa18956fe0b8e3eff46d882752363e52d9cb7ebba0e24f6a30d63c5c29",
    "ad641e0f88711ef1799beb9147baf643563273c399c9f5687272cbc7f86982ae":
        "e3006bba8004416ac367193099c448112b372af562a06ff8b4282b667e73f80e",
}
UPDATED_CATALOG = {
    # Both catalogs retain the name-slot revisions. The second corrects Galgar's shared ending.
    "DialogueIndex.lua": ("0833d2d247f3b72bc7a411b6719d273021df2c02495ce1adfb3b3f058b854ca8",
                          "800ba6e852af64a229148b1eb15ba934ddefa0ca034b68901c28bd6b656f554b"),
    "ManagedPlayback.json": "eea972cfd8e9aa034225f23649104a88d07560976b824956b9c939da0a31c2c8",
}


def verified_file(addons, relative, digest, limit):
    from lite_store_playback import safe_path

    path = safe_path(addons, relative)
    try:
        with path.open("rb") as stream:
            require(0 < path.stat().st_size <= limit)
            raw = stream.read(limit + 1)
    except FileNotFoundError as error:
        raise ContextUnavailable("A recording or dialogue file is missing from this WoW installation.") from error
    pins = digest if isinstance(digest, tuple) else (digest,)
    require(0 < len(raw) <= limit and sha256(raw).hexdigest() in pins)
    return raw


def _stock(addons, name, limit):
    return verified_file(addons, PACK / name, GAME_FILES[name], limit)


def _lookup(records, revision):
    for folder, seal in records.items():
        if seal["clock_contract_sha256"] == revision:
            return folder, seal
    return None


def _complete(entry, names, name, skip_fallback_names):
    if entry is None:
        return False
    parts = [part for part in entry["sequence"].values() if part.get("kind") == "name"]
    return parts and all(name in names.get(part.get("nameSlot"), {})
                         and not (skip_fallback_names and name == part.get("fallback")) for part in parts)


def _chosen_recipe(catalog, recipe_for, revision, name, names, skip_fallback_names):
    active = _lookup(catalog["seals"], revision)
    if active:
        current = recipe_for(revision)
        require(current)
        if not _complete(current, names, name, skip_fallback_names):
            for prior in catalog.get("retained", {}).get(revision, {}).values():
                require(not _complete(recipe_for(prior["clock_contract_sha256"]), names, name, skip_fallback_names))
        return *active, current
    for active_id, alternatives in catalog.get("retained", {}).items():
        found = _lookup(alternatives, revision)
        if found:
            # There is at most one retained recipe per active revision in the admitted release.
            require(len(alternatives) == 1 and not _complete(recipe_for(active_id), names, name, skip_fallback_names))
            entry = recipe_for(revision)
            require(_complete(entry, names, name, skip_fallback_names))
            return *found, entry
    raise ContextUnavailable("The saved take belongs to a dialogue revision that is unavailable here.")


def _repair_pins(record, pins):
    for part in record.get("replaced_parts", {}).values():
        pins[part["path"]] = part["sha256"]
    for sub in record.get("class_choices", {}).values():
        _repair_pins(sub, pins)


def _public_stock_mode(addons):
    from lite_store_playback import safe_path

    mode = 0
    for region in STOCK_REGIONS:
        pack = "SpokenQuestsAudio" + region
        path = safe_path(addons, Path(pack) / (pack + ".toc"))
        try:
            with path.open("rb") as stream:
                toc = stream.read(64 * 1024 + 1)
        except FileNotFoundError:
            toc = b""
        require(len(toc) <= 64 * 1024)
        toc = toc.removeprefix(b"\xef\xbb\xbf")
        version = re.search(rb"^[ \t]*##[ \t]*Version:[ \t]*([^\r\n]+)", toc, re.IGNORECASE | re.MULTILINE)
        mode = (mode << 1) | bool(version and version[1].strip() == b"2.7.3")
    return mode


def _current_catalog(addons):
    projection = json.loads(verified_file(addons, PACK / "RecordingContexts.json", CONTEXT_PROJECTION_SHA256, 8 * 1024**2))
    require(projection["format"] == "dmspeaks-reviewed-name-contexts-2"
            and tuple(projection["regions"]) == STOCK_REGIONS)
    verified_file(addons, PACK / "DMSpeaksSpokenNames.toc", projection["toc_sha256"], 128 * 1024)
    for module, fingerprint in projection["registries"].items():
        require(Path(module).name == module and module.endswith(".lua"))
        verified_file(addons, PACK / module, fingerprint, 48 * 1024**2)
    managed = verified_file(addons, PACK / "ManagedPlayback.json", projection["managed_sha256"], 32 * 1024**2)
    index = verified_file(addons, PACK / "DialogueIndex.lua", projection["index_sha256"], 48 * 1024**2)
    return projection, json.loads(managed), index


def _legacy_catalog(addons, program_id, reviewed_programs, skyborne_programs, updated_programs):
    release = UPDATED_CATALOG if program_id in updated_programs else GAME_FILES
    managed_pins = ((release["ManagedPlayback.json"], *REVIEWED_CATALOGS)
                    if program_id in reviewed_programs else release["ManagedPlayback.json"])
    managed = verified_file(addons, PACK / "ManagedPlayback.json", managed_pins, 32 * 1024**2)
    managed_id = sha256(managed).hexdigest()
    index_hash = release["DialogueIndex.lua"]
    if program_id in skyborne_programs:
        index_hash = (*index_hash, "8ca36cd09165435838a34c81a2a5d6e3d8b5b49578af608a08d9fc205d62de80",
                      "d4f20e4ade33380fd8e907647c6137e56a5773b0981d0db97bd1b5e1f09f4296",
                      "08963dc5f0de5078d17d62991ed23446f108ae8ecce9c99e0c57a2dacd38980b",
                      "a4132ce61f40f843726df4aa600d8949ee22241ee2c81bda529b2c88dea87efb",
                      "cbca712db5a8e4fb886b3ee204ba1c49ddf48bbbcc33ec66c6ccef19557bdcde")
    index = verified_file(addons, PACK / "DialogueIndex.lua", index_hash, 48 * 1024**2)
    supplemental = {}
    if managed_id in REVIEWED_CATALOGS:
        projection = json.loads(verified_file(addons, PACK / "RecordingContexts.json", REVIEWED_CATALOGS[managed_id], 8 * 1024**2))
        require(projection["format"] == "dmspeaks-reviewed-name-contexts-1" and projection["managed_sha256"] == managed_id)
        verified_file(addons, PACK / "DMSpeaksSpokenNames.toc", projection["toc_sha256"], 128 * 1024)
        for module, fingerprint in projection["registries"].items():
            require(Path(module).name == module and module.endswith(".lua"))
            if module == "DialogueIndex.lua":
                require(sha256(index).hexdigest() == fingerprint)
            elif module != "OriginalCuts.lua":
                verified_file(addons, PACK / module, fingerprint, 48 * 1024**2)
        supplemental = projection["recipes"]
    return supplemental, json.loads(managed), index


def _preview_join_repairs(addons, plan, groups):
    """Use a reviewed fused clip only for its exact original sequence."""
    result = []
    offset = 0
    while offset < len(plan):
        for group in groups:
            originals = [(Path(part["path"]), part["sha256"], part["samples"]) for part in group["parts"]]
            if plan[offset:offset + len(originals)] != originals:
                continue
            path = PACK / "JoinRepairs" / (group["sha256"] + ".ogg")
            try:
                verified_file(addons, path, group["sha256"], 32 * 1024**2)
            except ContextUnavailable:
                continue
            for original, digest, _ in originals:
                verified_file(addons, original, digest, 32 * 1024**2)
            result.append((path, group["sha256"], group["samples"]))
            offset += len(originals)
            break
        else:
            result.append(plan[offset])
            offset += 1
    return result


def _plan(addons, player, target, names):
    from lite_recording_review import selected_name
    from lite_store_playback import HASH, STORE_PACKS, name_audio_relative, safe_path, store_index_path

    revision, _ = target["slot"].split(":")
    name = selected_name(player)
    reviewed_programs = (CHATTER_PLAYBACK, ITEM_GUID_PLAYBACK, INVENTORY_INSTANCE_PLAYBACK, EDITION_TEXT_PLAYBACK)
    skyborne_programs = (SKYBORNE_PLAYBACK, QUEST_REPLAY_PLAYBACK, *reviewed_programs)
    typed_programs = (TYPED_FALLBACK_PLAYBACK, *skyborne_programs)
    updated_programs = (UPDATED_PLAYBACK, *typed_programs)
    program = verified_file(addons, PACK / "Playback.lua", (*GAME_FILES["Playback.lua"], *updated_programs, REVIEWED_JOIN_PLAYBACK), 128 * 1024)
    program_id = sha256(program).hexdigest()
    current = program_id == REVIEWED_JOIN_PLAYBACK
    skip_fallback_names = current or program_id in updated_programs
    projection = None
    if current:
        projection, catalog, index = _current_catalog(addons)
        mode_mask = 1 << _public_stock_mode(addons)
    else:
        supplemental, catalog, index = _legacy_catalog(addons, program_id, reviewed_programs, skyborne_programs, updated_programs)

    def native_for(media, seal):
        folder = store_index_path(addons, media, seal)
        native = json.loads(verified_file(addons, folder / "native-selection-metadata.json",
                                          seal["native_selection_metadata_sha256"], 2 * 1024**2))
        require(native["locale"] == "en-US" and native["npc_id"] == seal["npc_id"]
                and native["source_id"] == seal["source_id"])
        return native, folder

    if current:
        seals_by_revision = {seal["clock_contract_sha256"]: (media, seal)
                             for records in (catalog["seals"], *catalog.get("retained", {}).values())
                             for media, seal in records.items()}

    def recipe_for(key):
        if current:
            choices = projection["recipes"].get(key)
            if choices is None:
                return entry_for(index, key)
            active, = (option["entries"] for option in choices if option["modes"] & mode_mask)
            if not active:
                return None
            native, _ = native_for(*seals_by_revision[key])
            candidates = [entry for literal in active if (entry := read_table(literal.encode("utf-8")))
                          and entry["key"] == native["spoken_key"]
                          and entry.get("npc", native["npc_id"]) == native["npc_id"]]
            require(len(candidates) == 1)
            entry = candidates[0]
        else:
            if key not in supplemental:
                return entry_for(index, key)
            entry = read_table(supplemental[key].encode("utf-8"))
        require(entry.get("storeRevision") == key and entry.get("rate") == RATE)
        return entry

    media, seal, entry = _chosen_recipe(catalog, recipe_for, revision, name, names, skip_fallback_names)
    native, folder = native_for(media, seal)
    clock = json.loads(verified_file(addons, folder / "clock-contract.json", revision, 2 * 1024**2))
    require(native["spoken_key"] == entry["key"])
    if "npc" in entry:
        require(entry["npc"] == native["npc_id"])
    # The slot revision includes the source clock. The sealed speaker, exact wording, quest
    # event, gossip branch or follow-up model is already selected; never search by quest ID.
    values = player["values"]
    profile = profile_choices(values, name)
    cuts = original_choices(_stock(addons, "OriginalCuts.lua", 4 * 1024**2), revision)
    if current and cuts.get("races") and profile["race"] not in cuts["races"]:
        cuts = {}
    def has_typed_cut(clip):
        relative = PurePosixPath(clip["path"].replace("\\", "/"))
        require(relative.parts and relative.parts[0] in STORE_PACKS and ".." not in relative.parts)
        return safe_path(addons, relative).is_file()

    pieces = selected_pieces(entry, profile, names, cuts, target, skip_fallback_names=skip_fallback_names,
                             has_typed_cut=has_typed_cut if current or program_id in typed_programs else None)
    pins = {}
    for part in clock["sequence"]:
        for choice in part["choices"].values():
            filename = choice["file"]
            alias = seal.get("store_clip_paths", {}).get(filename, filename)
            pins[media + "/" + alias] = choice["sha256"]
    if entry.get("playbackRepair") or entry.get("playbackRepairByClass"):
        for record in json.loads(_stock(addons, "PlaybackRepairs.json", 1024 * 1024))["repairs"]:
            if record["revision"] == revision:
                _repair_pins(record, pins)
    plan = []
    for origin, clip in pieces:
        if origin == "personal":
            path, digest = name_audio_relative(clip), clip["sha256"]
        else:
            path = PurePosixPath(clip["path"].replace("\\", "/"))
            addresses = path.parts[1:2]
            stock = len(path.parts) >= 3 and path.parts[0] in STORE_PACKS
            word = current and len(path.parts) == 3 and path.parts[0] == PACK.name and addresses == ("Words",)
            require((stock or word) and ".." not in path.parts and path.suffix == ".ogg")
            digest = pins.get(path.as_posix())
            if (word or addresses in (("Playback",), ("OriginalCuts",))) and HASH.fullmatch(path.stem):
                digest = path.stem
        require(digest and HASH.fullmatch(digest) and type(clip["samples"]) is int and clip["samples"] > 0)
        plan.append((Path(path), digest, clip["samples"]))
    if projection is not None:
        overlay = json.loads(verified_file(addons, PACK / "JoinRepairs.json", projection["join_repairs_sha256"], 2 * 1024**2))
        require(overlay["format"] == "dmspeaks-join-repairs-1")
        plan = _preview_join_repairs(addons, plan, overlay["routes"].get(revision, ()))
    return plan


def render_audio_parts(addons, plan):
    import numpy as np
    import soundfile as sf

    require(all(type(samples) is int and samples > 0 for _, _, samples in plan)
            and 0 < sum(samples for _, _, samples in plan) <= MAX_FRAMES)
    result = BytesIO()
    with sf.SoundFile(result, "w", samplerate=RATE, channels=1, format="WAV", subtype="FLOAT") as output:
        for path, digest, samples in plan:
            data = verified_file(addons, path, digest, 32 * 1024**2)
            try:
                with sf.SoundFile(BytesIO(data)) as sound:
                    require((sound.format, sound.subtype, sound.samplerate, sound.channels, sound.frames)
                            == ("OGG", "VORBIS", RATE, 1, samples))
                    frames = 0
                    for block in sound.blocks(blocksize=32768, dtype="float32"):
                        require(np.isfinite(block).all() and frames + len(block) <= samples)
                        output.write(block)
                        frames += len(block)
                    require(frames == samples)
            except sf.LibsndfileError as error:
                raise ValueError("The installed dialogue audio could not be decoded.") from error
    return result.getvalue()


def context_audio(addons, player, target):
    """Return one bounded, in-memory, lossless WAV in game order for a server-resolved player."""
    from lite_recording_review import selected_name, selection
    from lite_store_playback import playable_saved_names

    target = selection(target)
    addons = Path(addons).resolve()
    names = playable_saved_names(addons)
    clip = names.get(target["slot"], {}).get(selected_name(player))
    if not clip or clip.get("sha256") != target["sha256"]:
        raise ValueError("This recording changed. Open its review again.")
    plan = _plan(addons, player, target, names)
    return render_audio_parts(addons, plan)
