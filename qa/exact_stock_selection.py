"""Exercise two recorded performances that share a quest, speaker and wording."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import lite_paced_name as paced
from lite_stock_text import generated_captions


class ExactStockSelection(unittest.TestCase):
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
