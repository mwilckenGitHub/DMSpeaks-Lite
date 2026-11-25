"""WoW race identity for character eligibility and spoken race choices."""

SKYBORNE_BY_ID = {95: "high order skyborne", 96: "windshaper skyborne"}
SKYBORNE_MASKS = {"high order skyborne": 1 << 32, "windshaper skyborne": 1 << 33}
SKYBORNE_FACTIONS = {"high order skyborne": "alliance", "windshaper skyborne": "horde"}

_RACE_ALIASES = {
    "nightelf": "night elf", "bloodelf": "blood elf", "scourge": "undead",
    "highorderskyborne": "high order skyborne", "skybornealliance": "high order skyborne",
    "windshaperskyborne": "windshaper skyborne", "skybornehorde": "windshaper skyborne",
}


def normalize_race(race, *, race_id=None, faction=None):
    """Return the eligibility key. The Forever race ID is locale-independent."""
    if type(race_id) is int and race_id in SKYBORNE_BY_ID:
        return SKYBORNE_BY_ID[race_id]
    value = str(race or "").strip().casefold().replace("-", " ")
    key = _RACE_ALIASES.get(value.replace(" ", "").replace("_", ""), value)
    if key == "skyborne":
        side = str(faction or "").strip().casefold()
        return next((variant for variant, group in SKYBORNE_FACTIONS.items() if group == side), key)
    return key


def profile_race(values):
    return normalize_race(values.get("race"), race_id=values.get("race_id"), faction=values.get("faction"))


def spoken_race(race):
    return "skyborne" if race in SKYBORNE_FACTIONS else race
