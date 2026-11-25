"""Skyborne identity, catalog authorization and faction-sensitive starter routes."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
from lite_context_routes import profile_choices
from lite_name_bridge import imported_character, load_settings, remember_character
from lite_portable_constraints import load_name_areas, matches_character, queue_areas
from lite_races import profile_race


class SkyborneEligibility(unittest.TestCase):
    def setUp(self):
        self.alliance = {"race": "Skyborne", "faction": "alliance", "class": "mage", "gender": "female", "level": 5}
        self.horde = {**self.alliance, "faction": "horde"}
        self.high = "high order skyborne"
        self.wind = "windshaper skyborne"
        self.row = {"stock_path": "SpokenQuestsAudioShared/shared.ogg", "source_id": "q:1:accept",
                    "npc_id": 12, "races": ["human"], "classes": ["mage"], "minimum_level": 4, "sexes": ["female"]}
        self.allowed = {self.row["stock_path"]: {"variants": [self.high], "required_classes_mask": 128}}

    def admits(self, profile, **kwargs):
        return matches_character(self.row, profile_race(profile), profile, kwargs.get("npc", {"12":"neutral"}),
                                 skyborne_eligibility=kwargs.get("map", self.allowed))

    def test_canonical_identity_and_spoken_choice(self):
        for values, variant in [
                (self.alliance, self.high), (self.horde, self.wind),
                ({"race": "Localized", "race_id":95}, self.high), ({"race": "Localized", "race_id":96}, self.wind),
                ({"race":"SkyborneAlliance"}, self.high), ({"race":"WindshaperSkyborne"}, self.wind),
                ({"race":"High Order Skyborne"}, self.high), ({"race":"Skyborne-Horde"}, self.wind)]:
            with self.subTest(values=values):
                self.assertEqual(profile_race(values), variant)
                self.assertEqual(profile_choices(values, "example")["race"], "skyborne")
                self.assertEqual(profile_choices(values, "example")["eligibility_race"], variant)
                self.assertEqual(queue_areas(variant, "starting-area"), ("zephras-isle",))
        self.assertEqual(profile_choices({"race":"NightElf"}, "sample")["race"], "night elf")
        self.assertEqual(queue_areas("human", "starting-area"), ("northshire",))

    def test_saved_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "DMSpeaksLite.lua"
            base = {"schema":2, "showProgress":True, "provider":"fish-s21-free", "starterOnly":True,
                    "playerName":"Sample", "playerRealm":"Home", "playerClass":"MAGE", "playerSex":"female"}
            player_id = None
            class FakeStore:
                def __init__(self): self.settings = {}
                def setting(self, key, default): return self.settings.get(key, default)
                def set_setting(self, key, value): self.settings[key] = value
            store = FakeStore()
            for fields, expected in [({"playerRace":"Skyborne", "playerFaction":"Alliance"}, self.high),
                    ({"playerRace":"Localized File Token", "playerRaceID":96, "playerFaction":"Horde"}, self.wind),
                    ({"playerRace":"Human", "playerFaction":"Alliance"}, "human")]:
                data = {**base, **fields}
                path.write_text("DMSpeaksLiteSettings = {\n" +
                    "".join(f'["{key}"] = {json.dumps(value,ensure_ascii=False)},\n' for key,value in data.items()) + "}\n",
                    encoding="utf-8")
                player = imported_character(load_settings(path))
                remember_character(store, player)
                self.assertEqual(expected, profile_race(store.settings["characters"][0]["values"]))
                if player_id is None: player_id = player["id"]
                self.assertEqual(player["id"], player_id)

    def test_exact_eligibility_and_restrictions(self):
        self.assertTrue(self.admits(self.alliance))
        self.assertFalse(self.admits(self.horde))
        self.assertFalse(self.admits({**self.horde, "race_id":95}))
        self.assertFalse(self.admits({"race":"Skyborne"}))
        self.assertFalse(self.admits(self.alliance, map={}))
        for spec in [{"gender":"male"}, {"class":"rogue"}, {"level":2}]:
            self.assertFalse(self.admits({**self.alliance, **spec}))
        self.assertFalse(self.admits(self.alliance, npc={"12":"horde"}))
        self.row["classes"] = ["mage","paladin"]
        self.assertFalse(self.admits({**self.alliance, "class":"paladin"}))
        self.row["quest_eligibility"]={"required_races_mask":1 << 33}
        self.assertFalse(self.admits(self.alliance))
        self.row["quest_eligibility"]={"required_races_mask":0}
        self.allowed[self.row["stock_path"]]["variants"].append(self.wind)
        self.assertTrue(self.admits(self.horde))
        self.row["companion_seed"] = {"addon":"DMSpeaksSpokenAlliance"}
        self.assertFalse(self.admits(self.horde))
        self.assertTrue(self.admits({**self.alliance,"race":"human"},map={}))
        self.assertFalse(self.admits({**self.alliance,"race":"gnome"},map={}))

    def test_area_override_requires_exact_catalog_and_known_key(self):
        with tempfile.TemporaryDirectory() as folder:
            addons = Path(folder).resolve()
            path = addons / "DMSpeaksNameData" / "NameFetch" / "NameAreas.json"
            path.parent.mkdir(parents=True)
            areas = {self.row["stock_path"]:"zephras-isle"}
            path.write_text(json.dumps({"format":1,"catalog_sha256":"valid","areas":areas}))
            self.assertEqual(load_name_areas(addons, "valid", [self.row]), areas)
            self.assertEqual(load_name_areas(addons, "old", [self.row]), {})
            self.assertEqual(load_name_areas(addons, "valid", []), {})

    def test_unknown_locations_stay_eligible_after_known_route_locations(self):
        catalog = ("rest", "coldridge-valley", "stormwind")
        self.assertEqual(queue_areas("gnome", "starting-area", catalog), ("coldridge-valley", "rest"))
        self.assertEqual(queue_areas("gnome", "route", catalog),
                         ("coldridge-valley", "dun-morogh", "loch-modan", "rest"))

    def test_alternate_speakers_can_place_one_recording_on_two_starting_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            addons = Path(folder).resolve()
            path = addons / "DMSpeaksNameData" / "NameFetch" / "NameAreas.json"
            path.parent.mkdir(parents=True)
            key = self.row["stock_path"]
            path.write_text(json.dumps({"format": 1, "catalog_sha256": "valid",
                                        "areas": {key: ["northshire", "coldridge-valley"]}}))
            self.assertEqual(load_name_areas(addons, "valid", [self.row], race="gnome")[key], "coldridge-valley")
            self.assertEqual(load_name_areas(addons, "valid", [self.row], race="human")[key], "northshire")


if __name__ == "__main__":
    unittest.main()
