"""Exercise two recorded performances that share a quest, speaker and wording."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import lite_paced_name as paced
from lite_portable_inputs import installed_plans, stock_revision_rows, verify_name_slots
from lite_portable_render import name_text
from lite_stock_text import generated_captions


class ExactStockSelection(unittest.TestCase):
    def test_marked_bare_n_is_replaced_without_sending_the_marker(self):
        for token in ("Adventurer", "N"):
            stock = f"Traits, {token}."
            row = dict(stock_text=stock, context=[0, len(stock)], slot=[8, 8 + len(token)],
                       source_cut=[10, 20], samples=40, leading_word=None)
            with self.subTest(token=token):
                verify_name_slots(row)
                self.assertEqual(name_text(row, "Dalamithiah"), ("Traits, Dalamithiah.", (8, 19)))

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.seals = {}
        self.words = "Greetings, $N."
        self.identity = {"source_id": "q:1:accept", "npc_id": 12, "locale": "enUS"}
        self.row = dict(self.identity, stock_path="sounds/second.ogg", samples=72,
                        source_text_sha256=sha256(self.words.encode()).hexdigest(),
                        companion_seed={"manifest_sha256": "2" * 64}, source_wave_sha256="b" * 64)
        for position, label, wave_digest in ((1, "first", "a"), (2, "second", "b")):
            folder = self.root / label
            folder.mkdir()
            native = str(position) * 64
            metadata = {"original_text": self.words, "source_samples": 72, "source_wave_sha256": wave_digest * 64,
                        "native_manifest_sha256": native, "original_spoken": {"path": "sounds/" + label + ".ogg"}}
            raw = json.dumps(metadata).encode()
            (folder / "native-selection-metadata.json").write_bytes(raw)
            (folder / "dialogue.json").write_text(json.dumps(dict(self.identity, source_native_manifest_sha256=native)))
            seal = dict(self.identity, native_selection_metadata_sha256=sha256(raw).hexdigest())
            self.seals[str(folder)] = seal

    def test_both_orders_select_the_recorded_performance(self):
        for keys in (list(self.seals), list(reversed(self.seals))):
            state = {"seals": {path: self.seals[path] for path in keys}}
            with self.subTest(order=keys), patch.object(paced, "validate_projection") as verify:
                folder, _, _, metadata = paced.prepared_base(None, self.row, state)
                self.assertEqual(folder.name, "second")
                self.assertEqual(metadata["original_spoken"]["path"], self.row["stock_path"])
                verify.assert_called_once()

    def test_absent_recording_does_not_select_its_sibling(self):
        first = str(self.root / "first")
        self.assertIsNone(paced.prepared_base(None, self.row, {"seals": {first: self.seals[first]}}))

    def test_same_path_recording_revisions_choose_the_exact_seal(self):
        first, second = self.seals
        old_pin, new_pin = "c" * 64, "d" * 64
        for path, pin in ((first, old_pin), (second, new_pin)):
            metadata_file = Path(path) / "native-selection-metadata.json"
            metadata = json.loads(metadata_file.read_bytes())
            metadata["original_spoken"] = {"path": self.row["stock_path"], "sha256": pin}
            raw = json.dumps(metadata).encode()
            metadata_file.write_bytes(raw)
            self.seals[path]["native_selection_metadata_sha256"] = sha256(raw).hexdigest()
        state = {"seals": self.seals}
        old_row = {**self.row, "stock_sha256": old_pin, "source_wave_sha256": "a" * 64,
                   "companion_seed": {"manifest_sha256": "1" * 64}}
        new_row = {**self.row, "stock_sha256": new_pin}
        with patch.object(paced, "validate_projection"):
            self.assertEqual(paced.prepared_base(None, old_row, state)[0].name, Path(first).name)
            self.assertEqual(paced.prepared_base(None, new_row, state)[0].name, Path(second).name)

        metadata_file = Path(second) / "native-selection-metadata.json"
        metadata = json.loads(metadata_file.read_bytes())
        metadata["reference_spoken"] = metadata["original_spoken"]
        metadata["original_spoken"] = None
        raw = json.dumps(metadata).encode()
        metadata_file.write_bytes(raw)
        self.seals[second]["native_selection_metadata_sha256"] = sha256(raw).hexdigest()
        with patch.object(paced, "validate_projection"):
            self.assertEqual(paced.prepared_base(None, old_row, state)[0].name, Path(first).name)
            self.assertEqual(paced.prepared_base(None, new_row, state)[0].name, Path(second).name)

    def test_changed_sealed_data_fails_before_projection(self):
        (self.root / "second" / "native-selection-metadata.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "sealed playback source changed"):
            paced.prepared_base(None, self.row, {"seals": self.seals})

    def test_expanded_check_only_matches_the_requested_performance(self):
        self.seals[str(self.root / "first")]["clock_format"] = paced.EXPANDED_FORMAT
        paced.require_name_extension(None, self.row, {"seals": self.seals})
        self.seals[str(self.root / "second")]["clock_format"] = paced.EXPANDED_FORMAT
        with self.assertRaisesRegex(ValueError, "expanded-context"):
            paced.require_name_extension(None, self.row, {"seals": self.seals})

class SpokenCaptionFormat(unittest.TestCase):
    def test_current_language_guard_keeps_caption_parsing_strict(self):
        table = b"""if not VoiceOver or not VoiceOver.DataModules then return end
if not VoiceOver.Language or (VoiceOver.Language.GetTextLanguage and VoiceOver.Language:GetTextLanguage() or VoiceOver.Language:GetClientLanguage()) ~= "enUS" then return end
SpokenQuestsAudioShared.TextByFileName = {
    ["1-accept"] = "Hello, Adventurer.",
}
"""
        pack = "SpokenQuestsAudioShared"
        self.assertEqual(generated_captions(table, pack), {"1-accept": "Hello, Adventurer."})
        with self.assertRaisesRegex(ValueError, "unfamiliar format"):
            generated_captions(table.replace(b'"enUS"', b'"deDE"'), pack)
        with self.assertRaisesRegex(ValueError, "unsupported entry"):
            generated_captions(table.replace(b'"Hello, Adventurer."', b'GetCaption()'), pack)


class SignedStockRevisions(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addons = Path(folder.name).resolve()
        pack = "SpokenQuestsAudioHorde"
        self.path = f"{pack}/generated/sounds/quests/1-accept.ogg"
        self.stock = self.addons / self.path
        self.stock.parent.mkdir(parents=True)
        self.table = self.addons / pack / "generated/text_table.lua"
        table = b'["1-accept"] = "Welcome, Adventurer.",\n'
        self.table.write_bytes(table)
        old = {"source_id": "q:1:accept", "npc_id": 40, "locale": "en-US",
               "races": ["orc"], "stock_path": self.path, "key": "1-accept",
               "stock_text": "Welcome, Adventurer.",
               "text_table_sha256": sha256(table).hexdigest(), "source_text_sha256": "a" * 64,
               "stock_sha256": sha256(b"original").hexdigest()}
        revised = {**old, "stock_sha256": sha256(b"revision").hexdigest()}
        self.old, self.new = old, revised
        self.variant = {"row": revised, "release": "b" * 64}
        self.catalog = {"dialogues": [old], "stock_revisions": {self.path: [self.variant]}}

    def test_stock_identity_selects_one_plan_in_either_direction(self):
        self.assertEqual(stock_revision_rows(self.catalog, self.catalog["dialogues"]),
                         [("b" * 64, self.new)])
        for content, chosen in ((b"original", self.old), (b"revision", self.new),
                                (b"unknown", self.old)):
            with self.subTest(content=content):
                self.stock.write_bytes(content)
                self.assertEqual(installed_plans(self.addons, self.catalog), [chosen])

    def test_revised_recording_also_needs_its_caption_table(self):
        self.stock.write_bytes(b"revision")
        self.table.write_bytes(b'["1-accept"] = "Different words.",\n')
        self.assertEqual(installed_plans(self.addons, self.catalog), [self.old])

    def test_a_revision_cannot_widen_the_original_race_scope(self):
        self.new["races"] = ["orc", "human"]
        with self.assertRaisesRegex(ValueError, "character scope"):
            stock_revision_rows(self.catalog, self.catalog["dialogues"])

    def test_reviewed_item_owner_needs_its_exact_recordings_and_release(self):
        path = "SpokenQuestsAudioHorde/generated/sounds/quests/79366-accept.ogg"
        old = {**self.old, "source_id": "q:79366:accept", "npc_id": 5905, "stock_path": path,
               "stock_sha256": "41b6837c6ffa10b168366f91a704e5ddfb3676029a2e16b6cef2ef419055fb6f"}
        revised = {**old, "npc_id": 268582, "giver_type": "Item",
                   "stock_sha256": "a4c272323f4751d058286816134a6985d610074812954e401174e4ea3554756c"}
        release = "b0a034463a891e3e21ff8181d36b73b183d3e74894d2e90a621dbdb34b8d9628"
        variant = {"row": revised, "release": release}
        catalog = {"dialogues": [old], "stock_revisions": {path: [variant]}}
        self.assertEqual(stock_revision_rows(catalog, [old]), [(release, revised)])
        for changed in ({"npc_id": 268583}, {"giver_type": "Creature"}, {"stock_sha256": "e" * 64}):
            with self.subTest(changed=changed):
                variant["row"] = {**revised, **changed}
                with self.assertRaisesRegex(ValueError, "dialogue owner"):
                    stock_revision_rows(catalog, [old])
        variant.update(row=revised, release="b" * 64)
        with self.assertRaisesRegex(ValueError, "dialogue owner"):
            stock_revision_rows(catalog, [old])

    def test_duplicate_recording_cannot_be_admitted_as_a_revision(self):
        self.new["stock_sha256"] = self.old["stock_sha256"]
        with self.assertRaisesRegex(ValueError, "distinct recording checksum"):
            stock_revision_rows(self.catalog, self.catalog["dialogues"])

    def test_defias_rename_is_bound_to_its_release_and_recordings(self):
        from lite_portable_inputs import _DEFIAS_FOLLOWUP_PRIOR, _DEFIAS_FOLLOWUP_PUBLISHER

        old = {**self.old, "source_id": "f:9:human-male-standard", "npc_id": 467,
               "stock_path": _DEFIAS_FOLLOWUP_PRIOR,
               "stock_sha256": "547d6653e10802118b9e85fcbaa1ae8aec6a99f4465f3c89616081daea3544f8"}
        revised = {**old, "stock_path": _DEFIAS_FOLLOWUP_PUBLISHER,
                   "stock_sha256": "d9741bd777e78a688178b7c0610721ae8469ae3efd1875b5a9ae74f5fe9b939f"}
        release = "b0a034463a891e3e21ff8181d36b73b183d3e74894d2e90a621dbdb34b8d9628"
        variant = {"release": release, "row": revised}
        catalog = {"dialogues": [old], "stock_revisions": {_DEFIAS_FOLLOWUP_PRIOR: [variant]}}
        self.assertEqual(stock_revision_rows(catalog, [old]), [(release, revised)])
        for change in ({"sexes": ["male"]}, {"stock_sha256": "a" * 64}, {"stock_path": self.path}):
            with self.subTest(change=change):
                variant["row"] = {**revised, **change}
                with self.assertRaisesRegex(ValueError, "recording path or character scope"):
                    stock_revision_rows(catalog, [old])
        variant.update(release="b" * 64, row=revised)
        with self.assertRaisesRegex(ValueError, "recording path or character scope"):
            stock_revision_rows(catalog, [old])

    def gendered_catalog(self):
        text = b'["m-1-accept"] = "Welcome, Adventurer.",\n["f-1-accept"] = "Welcome, Adventurer.",\n'
        self.table.write_bytes(text)
        previous = "$N spoke for himself.$B$BHave a seat."
        updated = "$N spoke for $Ghimself:herself;.\n\nHave a seat."
        self.old["source_text_sha256"] = sha256(previous.encode()).hexdigest()
        alternatives = []
        for prefix, gender in (("m-", "male"), ("f-", "female")):
            relative = self.stock.with_name(prefix + self.stock.name).relative_to(self.addons).as_posix()
            blob = gender.encode()
            (self.addons / relative).write_bytes(blob)
            row = {**self.old, "stock_path": relative, "key": prefix + self.old["key"], "sexes": [gender],
                   "stock_sha256": sha256(blob).hexdigest(), "text_table_sha256": sha256(text).hexdigest()}
            entry = {"release": "b" * 64, "row": row}
            if gender == "female":
                row["source_text_sha256"] = sha256(updated.encode()).hexdigest()
                entry["source_texts"] = [previous, updated]
            alternatives.append(entry)
        self.catalog["stock_revisions"][self.path] = alternatives
        return alternatives

    def test_gendered_recordings_keep_one_catalog_identity(self):
        from lite_source_identity import source_key

        alternatives = self.gendered_catalog()
        self.assertEqual(len(stock_revision_rows(self.catalog, [self.old])), 2)
        for gender, alternate in zip(("male", "female"), alternatives, strict=True):
            selected, = installed_plans(self.addons, self.catalog, gender=gender)
            self.assertEqual(selected["stock_sha256"], alternate["row"]["stock_sha256"])
            self.assertEqual(source_key(selected), self.path)
        self.assertEqual(installed_plans(self.addons, self.catalog), [self.old])
        (self.addons / alternatives[1]["row"]["stock_path"]).unlink()
        self.assertEqual(installed_plans(self.addons, self.catalog, gender="female"), [self.old])

    def test_gendered_revision_rejects_unrelated_wording_or_wrong_sex(self):
        _, female = self.gendered_catalog()
        female["row"]["sexes"] = ["male"]
        with self.assertRaisesRegex(ValueError, "character scope"):
            stock_revision_rows(self.catalog, [self.old])
        female["row"]["sexes"] = ["female"]
        female["source_texts"][1] = "$N spoke for $Ghimself:herself;.\n\nGo away."
        female["row"]["source_text_sha256"] = sha256(female["source_texts"][1].encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, "dialogue wording"):
            stock_revision_rows(self.catalog, [self.old])
