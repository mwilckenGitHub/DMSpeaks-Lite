"""Read a single WoW playback entry as literal data and select its ordered physical clips."""
from json import JSONDecoder
import re

from lite_races import profile_race, spoken_race


class ContextUnavailable(ValueError):
    """The installed files and known profile cannot establish this take's dialogue."""


def require(value):
    if not value:
        raise ContextUnavailable("The installed dialogue or character information is insufficient for this recording.")


# Indexed routes contain table literals only. Never load or execute game or add-on code.
_TOKEN = re.compile(r'\s*(?:(-?\d+\.?\d*)|([A-Za-z_]\w*)|([{}\[\]=,])|("))')
_JSON = JSONDecoder()


def read_table(raw):
    text = raw.decode("utf-8")
    position = 0

    def token():
        nonlocal position
        match = _TOKEN.match(text, position)
        require(match)
        position = match.end()
        number, symbol, punctuation, quote = match.groups()
        if quote:
            value, position = _JSON.raw_decode(text, position - 1)
            return "value", value
        if number:
            return "value", float(number) if "." in number else int(number)
        if symbol in ("true", "false"):
            return "value", symbol == "true"
        return ("key", symbol) if symbol else (punctuation, None)

    def value(item, depth):
        if item[0] == "value":
            return item[1]
        require(item[0] == "{" and depth < 15)
        result = {}
        ordinal = 0
        item = token()
        while item[0] != "}":
            if item[0] == "[":
                kind, key = token()
                require(kind == "value" and token()[0] == "]" and token()[0] == "=")
                item = token()
            elif item[0] == "key":
                key = item[1]
                require(token()[0] == "=")
                item = token()
            else:
                ordinal += 1
                key = ordinal
            result[key] = value(item, depth + 1)
            item = token()
            if item[0] != "}":
                require(item[0] == ",")
                item = token()
        return result

    result = value(token(), 0)
    require(isinstance(result, dict))
    return result


def entry_for(index, revision):
    field = b'storeRevision = "' + revision.encode("ascii") + b'"'
    pos = index.find(field)
    require(pos >= 0)
    start = index.rfind(b'\n    { key = ', 0, pos)
    end = index.find(b'\n', pos)
    require(start >= 0 and end > pos and end - start <= 1024 * 1024)
    # The index registers the same route under stock and standalone keys. The release
    # pins the index, including equality of each repeated revision's entry.
    entry = read_table(index[start + 1:end])
    require(entry.get("storeRevision") == revision and entry.get("rate") == 44100)
    return entry


def original_choices(raw, revision):
    marker = b'\n    ["' + revision.encode("ascii") + b'"] = '
    start = raw.find(marker)
    if start < 0:
        return {}
    end = raw.find(b'\n    },', start + len(marker))
    require(end > start and end - start <= 256 * 1024)
    return read_table(raw[start + len(marker):end + 6])


def profile_choices(values, name):
    race = profile_race(values)
    gender = values.get("gender")
    if isinstance(gender, str):
        gender = gender.strip().casefold()
    sex = {2: 2, 3: 3, "2": 2, "3": 3, "male": 2, "female": 3}.get(gender)
    job = str(values.get("class") or "").strip().casefold()
    job = {"death knight": "deathknight", "demon hunter": "demonhunter"}.get(job, job)
    speech = spoken_race(race)
    profile = {"name": name, "class": job, "race": speech, "sex": sex}
    if race != speech:
        profile["eligibility_race"] = race
    return profile


def selected_pieces(entry, profile, names, cuts, target, *, skip_fallback_names=False, has_typed_cut=None):
    for field, key in (("allowedRaces", "race"), ("allowedClasses", "class"), ("allowedSex", "sex")):
        if field in entry:
            value = profile.get("eligibility_race", profile[key]) if field == "allowedRaces" else profile[key]
            require(entry[field].get(value))
    require(not entry.get("playerSex") or profile["sex"] == entry["playerSex"])
    require(not entry.get("names") or entry["names"].get(profile["name"]))
    require(not entry.get("worldStateText"))
    named = entry.get("byName", {}).get(profile["name"])
    sequence = (named.get("sequence", named) if named else entry["sequence"])
    repair = None
    has_fetched = any(part.get("kind") == "name" and profile["name"] != part.get("fallback")
                      and names.get(part.get("nameSlot"), {}).get(profile["name"]) for part in sequence.values())
    if not named and not has_fetched:
        by_class = entry.get("playbackRepairByClass", {})
        if by_class:
            require(profile["class"])
        repair = by_class.get(profile["class"]) or entry.get("playbackRepair")
        if repair:
            for k in ("class", "race", "sex"):
                if k in repair:
                    require(profile[k])
                    if profile[k] != repair[k]:
                        repair = None
                        break
    require(0 < len(sequence) <= 256 and set(sequence) == set(range(1, len(sequence) + 1)))
    result = []
    matched = False
    for i in range(1, len(sequence) + 1):
        part = (repair or {}).get("parts", {}).get(i, sequence[i])
        clip, fetched, alternate = part.get("fixed"), None, None
        if not clip:
            kind = part.get("kind")
            if kind == "gender":
                require(profile["sex"])
                value = part["bySex"].get(profile["sex"])
            else:
                require(kind in ("class", "race", "name") and profile[kind])
                value = profile[kind]
            choices = part["choices"]
            if kind == "gender":
                require(value is not None and value in choices)
            if kind in ("race", "class") and value not in choices:
                require(part.get("fallback") == "adventurer")
            key = value if value in choices else part.get("fallback")
            clip = choices.get(key)
            if kind == "name" and part.get("nameSlot") and not (skip_fallback_names and value == part.get("fallback")):
                fetched = names.get(part["nameSlot"], {}).get(value)
            if not (fetched or named or repair):
                alternate = cuts.get(i, {}).get(key)
                if alternate and has_typed_cut and kind in ("class", "race") and not has_typed_cut(alternate):
                    alternate = None
        alternate = alternate or (repair or {}).get("audio", {}).get(i)
        if alternate:
            result.append(("stock", alternate))
        elif fetched:
            if "prefix" in fetched:
                result.append(("personal", fetched["prefix"]))
            result.append(("personal", fetched))
            if part["nameSlot"] == target["slot"] and fetched["sha256"] == target["sha256"]:
                matched = True
        else:
            require(clip)
            race = part.get("kind") == "race"
            if race and clip.get("prefix"):
                result.append(("stock", clip["prefix"]))
            result.append(("stock", clip))
            if race and clip.get("suffix"):
                result.append(("stock", clip["suffix"]))
    require(matched)
    return result
