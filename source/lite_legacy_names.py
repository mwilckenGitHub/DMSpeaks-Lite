"""Keep verified whole legacy name sequences beside newly admitted recipes."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re

import soundfile as sf

from lite_portable_io import read
from lite_portable_index import render_index_entry
from spoken_pack_lua import lua_string


LEGACY_PACKS = ("DMSpeaksSpokenAlliance", "DMSpeaksSpokenShared", "DMSpeaksSpokenHorde", "DMSpeaksSpokenGossip")
FIELDS = ("source_id", "original_text", "npc_id", "locale", "sample_rate",
          "source_wave_sha256", "source_samples", "original_spoken")
EVENT = {"accept": 1, "progress": 2, "complete": 3}
KINDS = {"fixed", "name", "class", "race", "gender"}


def lua_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return lua_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(f"[{lua_value(key)}]={lua_value(item)}" for key, item in value.items()) + "}"
    return "{" + ",".join(lua_value(item) for item in value) + "}"


def legacy_identity(manifest):
    return tuple(manifest.get(key) for key in ("source_id", "npc_id", "locale"))


def sequence(manifest, player, name):
    choices = {"name":name, "class":player.get("player_class"), "race":player.get("player_race")}
    parts = manifest.get("character_sequences", {}).get(name, manifest["sequence"])
    kinds = {part["kind"] for part in parts}
    if "name" not in kinds or not kinds <= KINDS:
        return None
    picked, records, cursor = [], [], 0
    for part in parts:
        kind = part["kind"]
        if kind == "fixed":
            piece = part["clip"]
        else:
            value = (part["value_by_player_sex"].get(str(player.get("player_sex")))
                     if kind == "gender" else choices[kind])
            specific = part["choices"].get(value)
            if kind == "name" and (not specific or value == "adventurer"):
                return None
            piece = specific or part["choices"][part["fallback"]]
        leaf, count = piece["file"], piece["samples"]
        if Path(leaf).name != leaf or "\\" in leaf or not leaf.endswith(".ogg") or type(count) is not int or count <= 0:
            return None
        picked.append(piece)
        records.append({"file":leaf, "samples":count, "start_sample":cursor})
        cursor += count
    if records == player.get("sequence") and cursor == player.get("total_samples"):
        return kinds, picked


class LegacyNames:
    def __init__(self, addons, addon_name):
        self.addon_name = addon_name
        self.existing = {}
        self.quest = {}
        self.followup = {}
        self.media = {}
        self.rejections = []
        self.addons = None
        self.typed_entries = []
        if addons is None:
            return
        self.addons = addons.resolve()
        for pack in LEGACY_PACKS:
            for path in sorted((self.addons / pack).glob("generated/sounds/*/*/dialogue.json")):
                if path.resolve() != path:
                    continue
                try:
                    record = read(path)
                    if record.get("verified_player_playback"):
                        self.existing.setdefault(legacy_identity(record), []).append((path.parent, record))
                except (OSError, ValueError, TypeError, AttributeError):
                    continue

    def write_media(self, output, folder, key, pieces):
        decoded, pending = [], {}
        cursor = 0
        for piece in pieces:
            path = folder / piece["file"]
            checksum, count = piece["sha256"], piece["samples"]
            if not re.fullmatch("[0-9a-f]{64}", checksum):
                raise ValueError("Legacy Ogg is missing its recorded hash.")
            relative = f"{self.addon_name}/legacy/{key}/{checksum}.ogg"
            previous_count = self.media.get(relative)
            if relative in pending:
                previous_count = pending[relative][0]
            if previous_count is not None and previous_count != count:
                raise ValueError("Legacy Ogg is listed with conflicting sample counts.")
            if previous_count is None:
                body = None
                candidates = (path, self.addons / relative) if self.addons is not None else (path,)
                for candidate in candidates:
                    if candidate.resolve() == candidate and candidate.is_file():
                        content = candidate.read_bytes()
                        if sha256(content).hexdigest() == checksum:
                            body = content
                            break
                if body is None:
                    raise ValueError("No legacy Ogg matches its recorded hash.")
                with sf.SoundFile(BytesIO(body)) as sound:
                    if (sound.format, sound.subtype, sound.samplerate, sound.frames) != ("OGG", "VORBIS", 44100, count):
                        raise ValueError("Legacy Ogg differs from its recorded sample clock.")
                    if sum(len(block) for block in sound.blocks(blocksize=32768)) != count:
                        raise ValueError("Legacy Ogg ended at a different sample.")
                pending[relative] = (count, body)
            decoded.append({"first":cursor,"last":cursor+count,
                            "fixed":{"path":relative.replace("/", "\\"),"samples":count}})
            cursor += count
        for relative, (count, body) in pending.items():
            output.writestr(relative, body)
            self.media[relative] = count
        return decoded

    def add(self, output, modern, metadata):
        route = modern.get("native_route") or {}
        kind = route.get("kind")
        key = modern["spoken_key"]
        if kind not in {"quest", "followup"} or key in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            return
        for folder, old in self.existing.get(legacy_identity(metadata), ()):
            if old.get("sample_rate") != 44100 or any(old.get(field) != metadata.get(field) for field in FIELDS):
                continue
            for name, player in sorted(old["verified_player_playback"].items()):
                try:
                    if player["name"].casefold() != name or player["sample_rate"] != 44100:
                        continue
                    selection = sequence(old, player, name)
                    if selection is None:
                        continue
                    kinds, pieces = selection
                    entry = {"key":key,"npc":metadata["npc_id"],"text":metadata["original_text"],
                             "event":EVENT[route["event"]] if kind=="quest" else 6,"rate":44100,
                             "names":{name:True},"legacyNameCaption":True}
                    if "class" in kinds:
                        entry["allowedClasses"] = {player["player_class"]:True}
                    if "race" in kinds:
                        entry["allowedRaces"] = {player["player_race"]:True}
                    if "gender" in kinds:
                        entry["allowedSex"] = {player["player_sex"]:True}
                    if kind == "quest":
                        entry["questID"] = int(metadata["source_id"].split(":")[1])
                    entry["sequence"] = self.write_media(output, folder, key, pieces)
                except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError) as error:
                    self.rejections.append({"source_id":metadata["source_id"],"name":name,"reason":str(error)})
                    continue
                index = self.quest if kind == "quest" else self.followup
                index.setdefault(key, []).append(entry)

    def add_preserved(self, output, modern, folder, previous):
        """Keep a sealed personalized sequence on its original sample clock."""
        route = modern["native_route"]
        key = modern["spoken_key"]
        if ({**route, "key": previous["native_route"]["key"]} != previous["native_route"]
                or route["kind"] not in {"quest", "followup"}
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", key)
                or {part["kind"] for part in previous["sequence"]} - KINDS):
            raise ValueError("A preserved Name no longer belongs to the same dialogue.")
        options = [set(part["choices"]) - {part["fallback"]}
                   for part in previous["sequence"] if part["kind"] == "name"]
        names = set.intersection(*options) if options else set()
        if not names:
            raise ValueError("A preserved Name needs every personalized occurrence.")
        archived = deepcopy(previous)
        archived.update(spoken_key=key, native_route=route)
        clips = [clip for part in archived["sequence"]
                 for clip in ((part["clip"],) if part["kind"] == "fixed" else part["choices"].values())]
        self.write_media(output, folder, key, clips)
        for clip in clips:
            clip["file"] = clip["sha256"] + ".ogg"
        table = "legacyNamedQuestVariants" if route["kind"] == "quest" else "legacyNamedFollowupVariants"
        entry = render_index_entry(archived, Path(self.addon_name) / "legacy" / key, None, names=names)
        self.typed_entries.append((table, key, entry.strip().removesuffix(",")))
        return names

    def write_index(self, output):
        if not (self.quest or self.followup or self.typed_entries):
            return False
        lines = ["local _, addon = ...",
                 "addon.legacyNamedQuestVariants = " + lua_value(self.quest),
                 "addon.legacyNamedFollowupVariants = " + lua_value(self.followup)]
        for table, key, entry in self.typed_entries:
            address = f"addon.{table}[{lua_string(key)}]"
            lines.append(f"{address} = {address} or {{}}")
            lines.append(f"table.insert({address}, 1, {entry})")
        output.writestr(f"{self.addon_name}/LegacyNames.lua", "\n".join(lines) + "\n")
        return True
