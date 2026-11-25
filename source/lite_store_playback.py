"""Read CurseForge-owned playback and publish generated Name choices."""
from copy import deepcopy
from hashlib import sha256
import json
import logging
from pathlib import Path, PurePosixPath
from unicodedata import category
import re

from lite_portable_constraints import name_clip_stem
from lite_clip_bounds import saved_cuts
from connection_files import replace_private
from lite_portable_io import digest, file_hash, hold, read
from lite_source_identity import dialogue_source_tuple
from spoken_pack_lua import lua_string

PLAYBACK = Path("DMSpeaksSpokenNames") / "ManagedPlayback.json"
STATUS = Path("DMSpeaksLiteStatus")
INDEX = STATUS / "Names.json"
OVERLAY_LOCK = ".dmspeaks-name-overlay.lock"
JOURNAL = STATUS / ".name-card-restore"
SLOT = re.compile(r"[0-9a-f]{64}:[1-9][0-9]*\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
STORE_PACKS = {"DMSpeaksSpoken" + scope for scope in ("Alliance", "Horde", "Shared", "Gossip")}
EVENT_FOLDERS = {"g": "gossip", "q": "quests", "f": "followup"}
GAME_KEY = re.compile(r"[A-Za-z0-9_.-]+\Z")


def media_folder(manifest, seal):
    event = EVENT_FOLDERS[manifest["source_id"].partition(":")[0]]
    stock = manifest.get("original_spoken") or {}
    folder = stock.get("path", "").partition("/")[0]
    scope = folder.removeprefix("SpokenQuestsAudio")
    if event == "gossip":
        scope = "Gossip"
    if "DMSpeaksSpoken" + scope not in STORE_PACKS:
        scope = "Shared"
    key = manifest["spoken_key"]
    if not GAME_KEY.fullmatch(key) or key in {".", ".."}:
        raise ValueError("A shared dialogue has an unsafe path.")
    return Path("DMSpeaksSpoken" + scope, "generated", "sounds", event,
                key + "-" + seal["clock_contract_sha256"][:12])


def stored_clip_path(folder, seal, filename):
    alias = seal.get("store_clip_paths", {}).get(filename, filename)
    if not isinstance(alias, str) or not GAME_KEY.fullmatch(alias) or not alias.endswith(".ogg"):
        raise ValueError("A sealed playback clip has an invalid path.")
    path = Path(folder) / alias
    if path.resolve() != path:
        raise ValueError("A sealed playback clip is stored outside its dialogue folder.")
    return path


def safe_path(addons, relative):
    path = addons / relative
    if not path.is_relative_to(addons) or path.resolve() != path:
        raise ValueError("A DMSpeaks file is outside the game add-ons folder.")
    return path


def same_game_dialogue(current, previous):
    return (dialogue_source_tuple(current) == dialogue_source_tuple(previous)
            and current["original_text"] == previous["original_text"]
            and current["native_route"] == previous["native_route"]
            and current.get("native_character_filters", {}) == previous.get("native_character_filters", {}))


def write_retained_recipes(output, current, revision, predecessors, *, decoded_clocks=None):
    """Keep old shared sequences that already have player-owned Name clips."""
    from lite_paced_delivery import validate_projection
    from lite_portable_index import render_index_entry

    records, index = {}, []
    for folder, seal in predecessors:
        folder = Path(folder).resolve()
        previous = read(folder / "dialogue.json")
        names = [part for part in previous["sequence"] if part["kind"] == "name"]
        if (revision == seal["clock_contract_sha256"] or not same_game_dialogue(current, previous)
                or not names):
            raise ValueError("A retained Name recipe must be an older version of the same dialogue.")
        if previous.get("character_sequences") or any(set(part["choices"]) != {"adventurer"} for part in names):
            raise ValueError("Retained shared playback cannot contain a player's name audio.")
        referenced = validate_projection(folder, previous, seal, decoded_clocks=decoded_clocks)
        route = media_folder(previous, seal)
        if route.as_posix() in records:
            raise ValueError("A Name recipe was retained twice for the same dialogue.")
        records[route.as_posix()] = seal
        for name in sorted(referenced | {"dialogue.json"}):
            file = stored_clip_path(folder, seal, name) if name.endswith(".ogg") else folder / name
            target = (route / file.name).as_posix()
            if target in output.NameToInfo:
                raise ValueError("A retained Name recipe overlaps another shared recipe.")
            output.write(file, target)
        rendered = deepcopy(previous)
        for part in rendered["sequence"]:
            for clip in (part["clip"],) if part["kind"] == "fixed" else part["choices"].values():
                clip["file"] = stored_clip_path(folder, seal, clip["file"]).name
        index.append(render_index_entry(rendered, route, None, store_seal=seal))
    return records, index


def store_index_path(addons, media, seal, *, check_folders=True):
    parts = PurePosixPath(media).parts
    if (not isinstance(seal, dict)
            or not isinstance(seal.get("clock_contract_sha256"), str)
            or not HASH.fullmatch(seal["clock_contract_sha256"])
            or len(parts) != 5 or parts[0] not in STORE_PACKS
            or parts[1:3] != ("generated", "sounds") or parts[3] not in EVENT_FOLDERS.values()
            or not GAME_KEY.fullmatch(parts[4])
            or not parts[4].endswith("-" + seal["clock_contract_sha256"][:12])):
        raise ValueError("The installed playback index contains an invalid dialogue.")
    relative = Path(*parts)
    return safe_path(addons, relative) if check_folders else addons / relative


def installed_playback(addons, *, check_folders=True):
    """Read admission records without copying, unpacking or rewriting shared files."""
    path = safe_path(addons, PLAYBACK)
    if not path.is_file():
        return {"seals": {}, "store_index_sha256": None}
    if path.stat().st_size > 32 * 1024**2:
        raise ValueError("The installed DMSpeaks playback index is too large.")
    raw = path.read_bytes()
    records = json.loads(raw)
    if not isinstance(records, dict) or records.get("format") != "dmspeaks-store-playback-1" or not isinstance(records.get("seals"), dict):
        raise ValueError("Update DMSpeaks for WoW in CurseForge to repair the playback index.")
    seals = {}
    seals_by_dialogue = {}
    active = {}
    for media, seal in records["seals"].items():
        directory = store_index_path(addons, media, seal, check_folders=check_folders)
        key = (seal.get("source_id"), seal.get("npc_id"))
        if not isinstance(key[0], str) or type(key[1]) is not int:
            raise ValueError("The installed playback index contains an invalid dialogue identity.")
        active[seal["clock_contract_sha256"]] = key
        # A missing folder affects this dialogue's coverage. Its indexed identity
        # still owns the retained revisions so the index can be validated.
        if check_folders and not directory.is_dir():
            continue
        location = str(directory)
        seals[location] = seal
        seals_by_dialogue.setdefault(key, []).append((location, seal))
    retained = {}
    prior_records = records.get("retained", {})
    if not isinstance(prior_records, dict):
        raise ValueError("The retained Name playback index is invalid.")
    for revision, predecessors in prior_records.items():
        if revision not in active or not isinstance(predecessors, dict):
            raise ValueError("A retained Name recipe has no active dialogue.")
        retained[revision] = []
        for media, seal in predecessors.items():
            path = store_index_path(addons, media, seal, check_folders=check_folders)
            if (active[revision] != (seal.get("source_id"), seal.get("npc_id"))
                    or seal["clock_contract_sha256"] in active):
                raise ValueError("A retained Name recipe does not match its active dialogue.")
            retained[revision].append((path, seal))
    toc = safe_path(addons, PLAYBACK.with_name("DMSpeaksSpokenNames.toc"))
    supports_segments = toc.is_file() and b"## X-DMSpeaks-NameSegments: 2" in toc.read_bytes().splitlines()
    return {"seals": seals, "seals_by_dialogue": seals_by_dialogue, "retained": retained, "name_segments": supports_segments,
            "store_index_sha256": digest(raw)}


def request_identity(row, spoken, provider):
    # A regional catalog update must only invalidate requests whose plan changed.
    payload = [row, spoken, provider]
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def saved_names(addons):
    index = safe_path(addons, INDEX)
    if not index.exists():
        return {}
    if index.stat().st_size > 32 * 1024**2:
        raise ValueError("The saved DMSpeaks Name index is too large.")
    contents = json.loads(index.read_bytes())
    if not isinstance(contents, dict):
        raise ValueError("The saved DMSpeaks Name index is invalid.")
    return contents


def slot_key(seal, index):
    return f'{seal["clock_contract_sha256"]}:{index}'


def name_audio_relative(clip):
    if "file" not in clip:
        checksum = clip["sha256"]
        if not HASH.fullmatch(checksum):
            raise ValueError("The saved player-name clip has an invalid checksum.")
        return STATUS / "names" / (checksum + ".ogg")
    relative = PurePosixPath(clip["file"])
    parts = relative.parts
    if (len(parts) != 6 or parts[:3] != (STATUS.name, "generated", "sounds")
            or parts[3] not in EVENT_FOLDERS.values() or not GAME_KEY.fullmatch(parts[4])
            or parts[4] in {".", ".."} or not parts[5].endswith(".ogg")
            or any(not (character.isalnum() or category(character).startswith("M") or character in "_.'-")
                   for character in parts[5])):
        raise ValueError("The saved player-name clip has an invalid placement path.")
    return Path(*parts)


def name_audio_routes(manifest, clock, seal, value, request, *, selected_slot=None):
    from lite_portable_package import PACED_GAME_PATH_LIMIT, paced_game_path_length

    event = EVENT_FOLDERS[manifest["source_id"].partition(":")[0]]
    stems, claims = {}, []
    for index, (before, after) in enumerate(zip(manifest["sequence"], clock["sequence"], strict=True), 1):
        if before["kind"] == "name" and (selected_slot is None or slot_key(seal, index) == selected_slot):
            stems[index] = name_clip_stem(manifest["spoken_key"], value, before["occurrence"]) + ".ogg"
            clip = after["choices"][value]
            segments = clip.get("segments")
            claims.append([segments["prefix"]["sha256"], segments["name"]["sha256"]] if segments else clip["sha256"])
    revision = digest(json.dumps([seal["clock_contract_sha256"], request, claims]).encode())[:20]
    root = STATUS / "generated" / "sounds" / event
    folder = root / f'{manifest["spoken_key"]}-{revision}'
    if any(paced_game_path_length(folder / stem) > PACED_GAME_PATH_LIMIT for stem in stems.values()):
        folder = root / revision
    if any(paced_game_path_length(folder / stem) > PACED_GAME_PATH_LIMIT for stem in stems.values()):
        raise ValueError("The player-name clip's filename exceeds the WoW playback path limit.")
    return {index: folder / stem for index, stem in stems.items()}


def playable_saved_names(addons):
    """Take one consistent snapshot of the name index for read-only coverage."""
    names = saved_names(addons)
    if not names:
        return names
    lua = safe_path(addons, STATUS / "Names.lua")
    return names if lua.is_file() and lua.read_bytes() == lua_index(names) else {}


def complete_saved_name(addons, names, manifest, seal, value):
    any_slot = False
    for index, part in enumerate(manifest["sequence"], 1):
        if part["kind"] != "name":
            continue
        any_slot = True
        choice = names.get(slot_key(seal, index), {}).get(value, {})
        if not HASH.fullmatch(choice.get("sha256", "")):
            return False
        for part in (choice, choice["prefix"]) if "prefix" in choice else (choice,):
            audio = safe_path(addons, name_audio_relative(part))
            if not audio.is_file() or file_hash(audio) != part["sha256"]:
                return False
    return any_slot


def name_present(addons, manifest, seal, value, state, *, playback_snapshot=None):
    from lite_paced_delivery import validate_projection

    names = playable_saved_names(addons) if playback_snapshot is None else playback_snapshot
    if not names:
        return False
    if complete_saved_name(addons, names, manifest, seal, value):
        return True
    for folder, prior_seal in state.get("retained", {}).get(seal["clock_contract_sha256"], ()):
        try:
            previous = read(safe_path(addons, Path(folder) / "dialogue.json"))
            if (not same_game_dialogue(manifest, previous)
                    or not complete_saved_name(addons, names, previous, prior_seal, value)):
                continue
            validate_projection(folder, previous, prior_seal, verify_pcm=False)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        return True
    return False


def lua_index(entries):
    lines = ["DMSpeaksFetchedNames = {"]
    for key, values in sorted(entries.items()):
        if not SLOT.fullmatch(key) or not isinstance(values, dict):
            raise ValueError("The saved DMSpeaks Name slot is invalid.")
        lines.append(f"    [{lua_string(key)}] = {{")
        for value, item in sorted(values.items()):
            checksum, samples = item["sha256"], item["samples"]
            if not isinstance(checksum, str) or not HASH.fullmatch(checksum) or type(samples) is not int or samples <= 0:
                raise ValueError("The saved DMSpeaks Name clip is invalid.")
            path = name_audio_relative(item).as_posix().replace("/", "\\")
            caption = lua_string(item["caption"])
            prefix_text = ""
            total = samples
            if prefix := item.get("prefix"):
                fixed = prefix.get("samples")
                if not HASH.fullmatch(prefix.get("sha256", "")) or type(fixed) is not int or fixed <= 0:
                    raise ValueError("The fixed prefix has an invalid decoded clock.")
                prefix_path = name_audio_relative(prefix).as_posix().replace("/", chr(92))
                prefix_text = f", prefix = {{ path = {lua_string(prefix_path)}, samples = {fixed} }}"
                total += fixed
            samples_text = ""
            cut_text = ""
            if cut := saved_cuts(item).get("cut"):
                cut_text = (f", cut = {{ startSample = {cut['start_sample']}, stopSample = {cut['stop_sample']}, "
                            f"sampleRate = {cut['sample_rate']}, donorSha256 = {lua_string(cut['donor_sha256'])} }}")
            if starts := item.get("starts"):
                if any(type(number) is not int or not 0 <= number < total for number in starts):
                    raise ValueError("The saved DMSpeaks Name caption clock is invalid.")
                samples_text = ", starts = {" + ", ".join(str(number) for number in starts) + "}"
            lines.append(f"        [{lua_string(value)}] = {{ path = {lua_string(path)}, samples = {samples}, "
                         f"caption = {caption}{samples_text}{prefix_text}{cut_text} }},")
        lines.append("    },")
    return ("\n".join([*lines, "}", ""])).encode("utf-8")


def write_placement(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_private(path, content)


def restore_loader(addons):
    """Finish publishing the committed name index if the companion was interrupted."""
    marker = safe_path(addons, JOURNAL)
    if marker.exists():
        with hold(addons, OVERLAY_LOCK):
            if marker.exists():
                write_placement(safe_path(addons, STATUS / "Names.lua"), lua_index(saved_names(addons)))
                marker.unlink()


def write_name_index(addons, entries):
    """Called while holding the overlay lock, after all referenced audio is in place."""
    loader = lua_index(entries)
    marker = safe_path(addons, JOURNAL)
    write_placement(marker, b"Rebuild Names.lua from the committed Names.json.")
    write_placement(safe_path(addons, INDEX), json.dumps(entries, ensure_ascii=False, sort_keys=True).encode())
    write_placement(safe_path(addons, STATUS / "Names.lua"), loader)
    marker.unlink()


def cleanup_folder(addons, queue):
    return queue / ".placed-returns" / digest(str(addons.resolve()).encode())


def pending_return_cleanup(addons, queue, seal, sequence, value, proofs):
    """Remember only local WAVs whose verified Ogg is about to be published."""
    slots = {p["occurrence"]:slot_key(seal, i) for i, p in enumerate(sequence, 1) if p["kind"] == "name"}
    markers = []
    for proof in proofs:
        ident = proof["request_sha256"]
        if not HASH.fullmatch(ident):
            raise ValueError("The returned recording has an invalid request identity.")
        directory = queue / ident
        receipt_path = directory / "receipt.json"
        try:
            if not receipt_path.is_file() or not (directory / "return.wav").is_file():
                continue
            receipt = read(receipt_path)
            if (receipt.get("state") != "returned" or receipt.get("retained") is not False
                    or receipt.get("request_sha256") != ident or receipt.get("wav_sha256") != proof["raw_sha256"]):
                continue
            marker = cleanup_folder(addons, queue) / (ident + ".json")
            pending = {"slot": slots[proof["occurrence"]], "name": value, "ogg": proof["clip"]["sha256"],
                       "wav": proof["raw_sha256"]}
            write_placement(marker, json.dumps(pending).encode())
            markers.append(marker)
        except (OSError, ValueError):
            logging.getLogger(__name__).warning("Could not save deferred WAV cleanup", exc_info=True)
    return markers


def finish_placed_returns(addons, queue, markers=None):
    """A cleanup failure must preserve the source WAV and allow the fetch to continue."""
    from audio_return_cache import finish_return
    try:
        if markers is None:
            markers = list(cleanup_folder(addons, queue).glob("*.json"))
        if not markers:
            return
        restore_loader(addons)
        entries = playable_saved_names(addons)
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("Could not check installed audio for WAV cleanup", exc_info=True)
        return
    for marker in markers:
        try:
            ident = marker.stem
            if not HASH.fullmatch(ident):
                continue
            pending = read(marker)
            history = queue / ident
            if not (history / "return.wav").is_file():
                marker.unlink(missing_ok=True)
                continue
            entry = entries.get(pending["slot"], {}).get(pending["name"], {})
            if entry.get("sha256") != pending["ogg"] or file_hash(safe_path(addons, name_audio_relative(entry))) != pending["ogg"]:
                continue
            with hold(history):
                receipt = read(history / "receipt.json")
                if (receipt.get("request_sha256") == ident and receipt.get("state") == "returned"
                        and receipt.get("wav_sha256") == pending["wav"]):
                    finish_return(history, receipt)
                marker.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError):
            logging.getLogger(__name__).warning("Could not finish a placed recording's WAV cleanup", exc_info=True)


def publish_names(addons, state, seal, current, clock, caption, value, payloads, *, request, replacement=None):
    """Activate a dialogue only after all of its verified Name occurrences are ready."""
    from lite_name_bridge import ensure_status_addon

    choices = {}
    files = {}
    selected_slot = replacement["slot"] if replacement else None
    routes = name_audio_routes(current, clock, seal, value, request, selected_slot=selected_slot)
    for index, (previous, updated) in enumerate(zip(current["sequence"], clock["sequence"], strict=True), 1):
        if index not in routes:
            continue
        clip = updated["choices"][value]
        binding = next(row for row in caption["bindings"] if row["sequence_index_1based"] == index)
        segments = clip.get("segments")
        if segments:
            prefix = segments["prefix"]
            name = segments["name"]
            files[name_audio_relative(prefix)] = (prefix["sha256"], payloads[prefix["sha256"]])
            content = payloads[name["sha256"]]
            starts = segments["starts"]
        else:
            name = clip
            content = payloads[clip["file"]]
            starts = binding.get("delivered_word_samples", {}).get(value)
        files[routes[index]] = (name["sha256"], content)
        entry = {"sha256": name["sha256"], "samples": name["samples"], "request": request,
                 "file": routes[index].as_posix(), "caption": binding["choices"][value], "starts": starts}
        entry.update(saved_cuts(name))
        if phrase := clip.get("context_phrase"):
            start, end = phrase["donor_samples"]
            if (segments or phrase["source"] != "returned_wav" or type(start) is not int
                    or type(end) is not int or start < 0 or end - start != name["samples"]
                    or not HASH.fullmatch(phrase["returned_sha256"])
                    or not HASH.fullmatch(phrase["paced_return_sha256"])):
                raise ValueError("The saved phrase must match one continuous cut from the returned WAV.")
            entry["provenance"] = phrase
        elif segments:
            entry.update(prefix=prefix, provenance={k: v for k, v in segments.items() if k not in {"prefix", "name", "starts"}})
        choices[slot_key(seal, index)] = entry
    if any(digest(content) != checksum for checksum, content in files.values()):
        raise ValueError("A verified Name clip changed before placement.")
    if not choices:
        raise ValueError("This dialogue has no verified Name occurrences.")
    restore_loader(addons)
    with hold(addons, OVERLAY_LOCK):
        if file_hash(safe_path(addons, PLAYBACK)) != state["store_index_sha256"]:
            raise ValueError("The CurseForge playback pack changed during the fetch. Try again.")
        if replacement:
            from lite_recording_review import current_take
            current_take(addons, value, replacement)
        elif name_present(addons, current, seal, value, state):
            return
        ensure_status_addon(addons)
        entries = saved_names(addons)
        for key, item in choices.items():
            entries.setdefault(key, {})[value] = item
        for relative, (checksum, content) in files.items():
            path = safe_path(addons, relative)
            if not path.is_file() or file_hash(path) != checksum:
                write_placement(path, content)
                if file_hash(path) != checksum:
                    raise ValueError("The saved name Ogg differs from the verified recording.")
        write_name_index(addons, entries)


def review_saved_returns(addons, row, manifest, seal, name, spoken, provider, previous, *,
                         confirmed=False, playback_snapshot=None):
    """Inspect a coverage snapshot, or detach contradictory local takes during a fetch."""
    value = name.casefold()
    expected = request_identity(row, spoken, provider)
    slots = [(slot_key(seal, index), part["occurrence"]) for index, part in enumerate(manifest["sequence"], 1)
             if part["kind"] == "name"]
    contradictions = previous.contradictions(row, spoken, provider)
    if not contradictions:
        return False
    def matching_receipts(entry, occurrence):
        if entry and entry.get("request") == expected:
            recorded_sha = entry.get("provenance", {}).get("returned_sha256")
            for directory, receipt, number in contradictions:
                if number == occurrence and (not recorded_sha or recorded_sha == receipt["wav_sha256"]):
                    yield directory, receipt

    if playback_snapshot is not None:
        return any(any(matching_receipts(playback_snapshot.get(key, {}).get(value), occurrence))
                   for key, occurrence in slots)
    from lite_portable_io import save

    flagged = False
    restore_loader(addons)
    with hold(addons, OVERLAY_LOCK):
        entries = saved_names(addons)
        changed = False
        for key, occurrence in slots:
            entry = entries.get(key, {}).get(value)
            for directory, receipt in matching_receipts(entry, occurrence):
                recorded_sha = entry.get("provenance", {}).get("returned_sha256")
                flagged = True
                if not recorded_sha and not confirmed:
                    # Older unsegmented names lack a donor hash. Require the
                    # player's explicit retake before changing that selection.
                    continue
                with hold(directory):
                    if read(directory / "receipt.json") != receipt:
                        continue
                    save(directory / "hold.json", {"wav_sha256": receipt["wav_sha256"],
                                                   "reason": "Previously saved provider words contradict the requested name."})
                    save(directory / ("previous-route-" + entry["sha256"] + ".json"),
                         {"slot": key, "name": value, "entry": entry})
                del entries[key][value]
                if not entries[key]:
                    del entries[key]
                changed = True
                break
        if changed:
            write_name_index(addons, entries)
    return flagged
