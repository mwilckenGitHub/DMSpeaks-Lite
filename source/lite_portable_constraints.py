"""Shared name spelling, route and response constraints."""
import io
import json
import re
import struct
from unicodedata import category, normalize
import wave

from guide_bindings import RATE
from lite_races import SKYBORNE_FACTIONS, SKYBORNE_MASKS, normalize_race

AREA_ORDER = ("northshire", "elwynn-forest", "valley-of-trials", "shadowglen", "stormwind", "orgrimmar")
CLASS_MASKS = dict(warrior=1, paladin=2, hunter=4, rogue=8, priest=16, shaman=64,
                   mage=128, warlock=256, druid=1024)


STARTING_ROUTES = {
    "human": ("northshire", "elwynn-forest", "stormwind", "westfall", "redridge-mountains", "duskwood"),
    "orc": ("valley-of-trials", "durotar", "orgrimmar", "the-barrens"),
    "troll": ("valley-of-trials", "durotar", "orgrimmar", "the-barrens"),
    "dwarf": ("coldridge-valley", "dun-morogh", "loch-modan"),
    "gnome": ("coldridge-valley", "dun-morogh", "loch-modan"),
    "night elf": ("shadowglen", "teldrassil", "darkshore"),
    "undead": ("deathknell", "tirisfal-glades", "silverpine-forest"),
    "tauren": ("camp-narache", "mulgore", "the-barrens"),
    "high order skyborne": ("zephras-isle",),
    "windshaper skyborne": ("zephras-isle",),
}

# Kaltunk offers "Your Place in the World" before the player meets Gornek.
OPENING_NPCS = {"valley-of-trials": 10176}


def ordered_area_dialogues(area, rows):
    first = OPENING_NPCS.get(area)
    return sorted(rows, key=lambda row: row["npc_id"] != first) if first is not None else rows


# The corpus lists this starter interaction as unzoned gossip.
STARTING_GREETINGS = {"tauren": (2980, "g:20e50f7907859f3c67714458b3ebf738")}


def starting_greeting_area(row, race):
    if STARTING_GREETINGS.get(race) == (row["npc_id"], row["source_id"]):
        return STARTING_ROUTES[race][0]
    return None


RACE_FACTIONS = {
    "human": "alliance", "dwarf": "alliance", "gnome": "alliance", "night elf": "alliance",
    "orc": "horde", "troll": "horde", "undead": "horde", "tauren": "horde",
    **SKYBORNE_FACTIONS,
}

FACTION_PACKS = {
    "SpokenQuestsAudioAlliance": "alliance", "SpokenQuestsAudioHorde": "horde",
    "DMSpeaksSpokenAlliance": "alliance", "DMSpeaksSpokenHorde": "horde",
}

NPC_FACTIONS = "NpcFactions.json"
REACTIONS = {"alliance", "horde", "neutral", "hostile"}


def companion_leading_word(leading, row):
    """Rebind checked seed character offsets after signed spacing/case changes."""
    source = row.get("companion_source_text")
    if source is None or leading is None:
        return leading
    if not isinstance(source, str):
        raise ValueError("The companion coordinate binding needs its source transcript.")
    before = list(re.finditer(r"\S+", source))
    after = list(re.finditer(r"\S+", row["stock_text"]))
    if [word.group().casefold() for word in before] != [word.group().casefold() for word in after]:
        raise ValueError("The companion coordinate binding changes its spoken words.")
    lo, hi = leading["source_chars"]
    for original, signed in zip(before, after, strict=True):
        if original.start() <= lo < hi <= original.end():
            start, end = signed.start() + lo - original.start(), signed.start() + hi - original.start()
            if source[lo:hi] != leading["source_text"]:
                raise ValueError("The companion leading word differs from its source transcript.")
            rebound = row["stock_text"][start:end]
            if rebound.casefold() != leading["source_text"].casefold():
                raise ValueError("The companion coordinate binding changes its leading word.")
            return {**leading, "source_chars": [start, end], "source_text": rebound}
    raise ValueError("The companion leading word has no matching signed transcript span.")


def name_article_source(row):
    leading = row.get("leading_word") or {}
    word = leading.get("source_text", "")
    if word.casefold() not in {"a", "an"} or "source_chars" not in leading:
        return None
    left, right = leading["source_chars"]
    stock = row["stock_text"]
    separator = stock[right:row["slot"][0]].replace("\r\n", "\n").replace("\r", "\n")
    if (row["context"][0] <= left < right <= row["slot"][0] and stock[left:right] == word
            and not separator.strip() and not re.search(r"\n[ \t]*\n", separator)):
        return {"source_chars": [left, right], "source": word}
    return None


def name_fallback_article_changed(slot):
    """Identify a measured a/an correction owned by the fallback Name clip."""
    leading = slot.get("leading_word")
    if not leading or not name_article_source(slot):
        return False
    end = leading["source_chars"][1]
    left = slot["slot"][0]
    return (slot["stock_text"][end:left].isspace()
            and {leading["source_text"].casefold(), leading["spoken_text"].casefold()} == {"a", "an"})


def leading_word_count(slot):
    leading = slot.get("leading_word")
    if not leading:
        return 0
    from lite_portable_render import count

    return count(leading["source_text"])


def name_occurrences(row):
    if "name_slots" not in row:
        slots = [row]
    else:
        slots = [{**row, **slot, "name_occurrence": occurrence}
                 for occurrence, slot in enumerate(row["name_slots"], 1)]
    result = []
    counts = row.get("paced_base_analysis", {}).get("join_windows", {}).get("leading_word_counts", [])
    for index, slot in enumerate(slots):
        leader = slot.get("leading_word")
        if (isinstance(leader, dict) and isinstance(leader.get("source_chars"), list)
                and len(leader["source_chars"]) == 2):
            before = leader["source_chars"][0]
            lo, hi = slot["context"]
            if isinstance(before, int) and 0 <= before < lo:
                slot = {**slot, "context": [before, hi]}
                if "request_context" in slot:
                    slot["request_context"] = slot["stock_text"][before:lo] + slot["request_context"]
        if row.get("native_source") and isinstance(counts, list) and len(counts) == len(slots):
            slot = native_paced_request_context(slot, counts[index])
        result.append(slot)
    return result


def native_paced_request_context(slot, leading):
    """Include sealed leading speech while preserving the admitted capsule row."""
    if type(leading) is not int or leading <= leading_word_count(slot):
        return slot
    from lite_portable_render import count

    text = slot["stock_text"]
    lo, hi = slot["context"]
    first, _ = slot["slot"]
    target = count(text[:first]) - leading
    if count(text[:lo]) <= target:
        return slot
    offsets = [match.start() for match in re.finditer(r"[^\W_]+(?:['’][^\W_]+)*", text[:lo])
               if count(text[:match.start()]) == target]
    if len(offsets) != 1:
        raise ValueError("The wider Name request has no exact source character boundary.")
    before, = offsets
    extended = {**slot, "context": [before, hi]}
    if "request_context" in slot:
        extended["request_context"] = text[before:lo] + slot["request_context"]
    return extended


def decode_return(raw):
    # The endpoint can use streaming RIFF length placeholders in a finite response.
    if len(raw) >= 44 and raw[:4] == b"RIFF" and raw[8:16] == b"WAVEfmt " and raw[36:40] == b"data":
        riff_size, data_size = struct.unpack_from("<I", raw, 4)[0], struct.unpack_from("<I", raw, 40)[0]
        if (riff_size, data_size) == (0xFFFFFF24, 0xFFFFFF00):
            fixed = bytearray(raw)
            struct.pack_into("<I", fixed, 4, len(raw) - 8)
            struct.pack_into("<I", fixed, 40, len(raw) - 44)
            raw = bytes(fixed)
    with wave.open(io.BytesIO(raw), "rb") as sound:
        count = sound.getnframes()
        if (sound.getframerate(), sound.getnchannels(), sound.getsampwidth()) != (RATE, 1, 2):
            raise ValueError("The return must be 44.1 kHz, mono and 16-bit PCM.")
        if not 0 < count <= 60 * 44100 or len(sound.readframes(count)) != 2 * count:
            raise ValueError("Fish returned incomplete audio or an unexpected duration.")
    return raw, count

def queue_areas(race, coverage, catalog_areas=()):
    race = normalize_race(race)
    route = STARTING_ROUTES.get(race)
    catalog_areas = tuple(dict.fromkeys(catalog_areas))
    if route and race in ("undead", "tauren") and catalog_areas and route[0] not in catalog_areas:
        route = route[1:]
    if route is None:
        if coverage != "all":
            raise ValueError("No starting route is available for this race. Choose All Available Dialogues.")
        route = ()
    if coverage in ("starting-area", "route"):
        selected = route[:1] if coverage == "starting-area" else route
        # An unknown location is insufficient evidence to exclude a dialogue.
        # Known local speakers keep their priority and character filters still apply.
        return (*selected, "rest") if "rest" in catalog_areas else selected
    # A route with no known location should not run before a named zone.
    return tuple(dict.fromkeys((*route, *AREA_ORDER, *(area for area in catalog_areas if area != "rest"), "rest")))


def load_name_areas(addons, catalog_sha256, rows, *, race=None):
    """Apply optional exact-catalog zone assignments without altering a plan's identity."""
    path = addons / "DMSpeaksNameData/NameFetch/NameAreas.json"
    try:
        if path.resolve() != path or not path.is_file():
            return {}
        with path.open("rb") as source:
            raw = source.read(512_001)
        if len(raw) > 512_000:
            return {}
        data = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        return {}
    if (not isinstance(data, dict) or type(data.get("format")) is not int or data["format"] != 1
            or data.get("catalog_sha256") != catalog_sha256 or not isinstance(data.get("areas"), dict)):
        return {}
    from lite_source_identity import source_key

    areas = data["areas"]
    known = {source_key(row) for row in rows}
    if not areas.keys() <= known:
        return {}
    route = STARTING_ROUTES.get(normalize_race(race), ()) if race else ()
    resolved = {}
    for key, value in areas.items():
        locations = value if isinstance(value, list) else [value]
        if not 1 <= len(locations) <= 64 or any(
                not isinstance(area, str) or len(area) > 80 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", area)
                for area in locations):
            return {}
        # An alternate quest giver can deliver the same recording on a different continent.
        resolved[key] = next((area for area in route if area in locations), locations[0])
    return resolved


def load_npc_factions(addons, catalog_sha256):
    for module in ("DMSpeaksNameData", "DMSpeaksLite"):
        folder = addons / module / "NameFetch"
        if not (folder / "Prepared-dialogue-plans.zip").is_file():
            continue
        path = folder / NPC_FACTIONS
        if not (path.exists() or path.is_symlink()):
            if module == "DMSpeaksNameData":
                raise ValueError("Update DMSpeaks to get the NPC faction data before fetching names.")
            return None
        if path.resolve() != path or not path.is_file() or path.stat().st_size > 512_000:
            raise ValueError("Update DMSpeaks to repair its NPC faction data.")
        try:
            data = json.loads(path.read_bytes())
        except (UnicodeError, ValueError) as error:
            raise ValueError("Update DMSpeaks to repair its NPC faction data.") from error
        if (not isinstance(data, dict) or data.get("format") != 1
                or data.get("catalog_sha256") != catalog_sha256 or not isinstance(data.get("npcs"), dict)):
            raise ValueError("Update DMSpeaks to get the NPC faction data for this dialogue catalog.")
        npcs = data["npcs"]
        if any(not re.fullmatch(r"[1-9][0-9]{0,9}", key) or not isinstance(value, str)
               or value not in REACTIONS for key, value in npcs.items()):
            raise ValueError("Update DMSpeaks to repair its NPC faction data.")
        return npcs
    return None


def matches_character(row, race, values, npc_factions=None, *, skyborne_eligibility=None):
    level = values.get("level")
    faction = str(values.get("faction", "")).casefold() or RACE_FACTIONS.get(race)
    job = str(values.get("class", "")).casefold()
    if race in SKYBORNE_FACTIONS:
        from lite_source_identity import source_key

        if faction != SKYBORNE_FACTIONS[race] or not skyborne_eligibility:
            return False
        allowed = skyborne_eligibility.get(source_key(row))
        if not allowed or race not in allowed["variants"]:
            return False
        if allowed["required_classes_mask"] and not (allowed["required_classes_mask"] & CLASS_MASKS.get(job, 0)):
            return False
        mask = (row.get("quest_eligibility") or {}).get("required_races_mask", 0)
        if mask and not (mask & SKYBORNE_MASKS[race]):
            return False
    elif race == "skyborne" or race not in row["races"]:
        return False
    stock_pack = row.get("stock_path", "").partition("/")[0]
    companion_pack = (row.get("companion_seed") or {}).get("addon")
    for pack in (stock_pack, companion_pack):
        required = FACTION_PACKS.get(pack)
        if required and required != faction:
            return False
    is_object = row.get("game_object_reference") or (row.get("native_route") or {}).get("giver_type") == "GameObject"
    if npc_factions is not None and not is_object:
        reaction = npc_factions.get(str(row.get("npc_id")))
        if reaction not in ("neutral", faction) or reaction in (None, "hostile"):
            return False
    return (("minimum_level" not in row or type(level) is int and level >= row["minimum_level"])
            and ("classes" not in row or job in row["classes"])
            and ("sexes" not in row or str(values.get("gender", "")).casefold() in row["sexes"]))

def validate_name(value):
    if isinstance(value, str):
        value = normalize("NFC", value)
    if not isinstance(value, str) or len(value) > 80 or not re.fullmatch(
            r"[^\W\d_]+(?:[-'][^\W\d_]+)*", value):
        raise ValueError("Provide the character's first name as literal text.")
    return value


def name_clip_stem(dialogue, value, occurrence):
    if (not isinstance(dialogue, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", dialogue)
            or dialogue in {".", ".."} or type(occurrence) is not int or occurrence < 1):
        raise ValueError("The player-name clip needs a valid dialogue and occurrence.")
    # Unicode case folding can leave combining marks in a valid character name.
    if (not isinstance(value, str) or not value
            or any(not (character.isalpha() or category(character).startswith("M") or character in "-'")
                   for character in value)):
        raise ValueError("The player-name clip needs a valid filename spelling.")
    return f"{dialogue}-name-{value}-{occurrence}"
