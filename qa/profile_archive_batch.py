"""Check atomic profile archiving using the companion's actual SQLite settings."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
from lite_game_ingress import GameIngress
from settings_store import SettingsStore


class ProfileArchiveBatch(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SettingsStore(directory.name)
        self.addCleanup(self.store.close)
        self.ingress = GameIngress.__new__(GameIngress)
        self.ingress.root = Path(directory.name) / "game"
        self.ingress.app = SimpleNamespace(store=self.store)
        self.ingress.request_lock = RLock()
        self.ingress.profile_lock = RLock()
        self.ingress.name_recording_counts = Mock(return_value={"ana": 12, "bert": 3})
        self.ingress.active = None
        self.known = {name: {"name": name, "realm": "Realm", "race": "Human", "class": "Mage"}
                      for name in ("Ana", "Bert", "Cora")}
        self.key = "lite_wow_profiles_" + str(self.ingress.root)
        self.store.set_setting(self.key, self.known)

    def remove(self, ids):
        return self.ingress.change_profile_removal({"character_ids": ids, "removed": True})

    def test_remove_selected_and_restore_one(self):
        self.assertEqual(set(self.remove(["Ana", "Cora"])), {"Ana", "Cora"})
        self.assertEqual(set(self.remove(["Ana", "Cora"])), {"Ana", "Cora"})
        restored = self.ingress.change_profile_removal({"character_id": "Ana", "removed": False})
        self.assertEqual(set(restored), {"Cora"})
        self.assertEqual(self.store.setting(self.key), self.known)

    def test_failure_does_not_archive_any_of_the_selected_profiles(self):
        self.remove(["Cora"])
        for ids in ([], ["Ana", "missing"], ["Ana", None]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.remove(ids)
            self.assertEqual(set(self.ingress.removed_profiles()), {"Cora"})
        self.ingress.active = ("job", "request", "fetch", None, "Bert", {}, None)
        with self.assertRaisesRegex(ValueError, "current fetch"):
            self.remove(["Ana", "Bert"])
        self.assertEqual(set(self.ingress.removed_profiles()), {"Cora"})


if __name__ == "__main__":
    unittest.main()
