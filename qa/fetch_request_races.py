"""Exercise overlapping fetch controls and SQLite contention using disposable settings."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_session import GameIngress, Meta, SETTINGS, SettingsStore, listener, request
from lite_game_ingress import INFLIGHT, LAST_FETCH


class RequestRaces(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = SettingsStore(self.root)
        self.addCleanup(self.store.close)
        self.game = game = GameIngress.__new__(GameIngress)
        game.root = self.root
        game.store_managed = True
        game.app = SimpleNamespace(store=self.store, pause_player_name_fetch=Mock())
        game.active = None
        game.request_lock, game.profile_lock = Lock(), Lock()
        game.options = Mock(return_value={"provider_id": "fish-s21-free", "coverage": "route", "allow_metered": False})
        self.player = {"id": "alice", "values": {"name": "Alice"}}
        game.popup_player = Mock(return_value=(None, self.player))
        game.tick = Mock()
        game.finish_active = Mock()
        game.popup_fetch_status = lambda job, owner: {"state": "running" if job else "idle", "job_id": job}
        def begin(path, player, values, options, request_id, action, **kwargs):
            game.active = ("new-job", request_id, "fetch", None, player["id"], values)
            self.store.set_setting(INFLIGHT, {"request_id": request_id, "job": dict(options)})
            return "new-job"
        game.begin_fetch = Mock(side_effect=begin)
        self.port = self.enterContext(listener(game, self.root))
        self.headers = {"Content-Type": "application/json", "X-Lite-Token": Meta(request(self.port, "/")[1]).values["lite-token"]}

    def send(self, action, job_id=None, **kwargs):
        data = {"action": action, "job_id": job_id, "character_id": "alice", **self.game.options(), **kwargs}
        status, body = request(self.port, SETTINGS+"fetch", data=data, headers=self.headers)
        return status, json.loads(body)

    def test_two_starts_and_crossed_controls(self):
        barrier = Barrier(2)
        def start():
            barrier.wait()
            return self.send("start")
        with ThreadPoolExecutor(2) as pool:
            first, second = [pool.submit(start) for _ in range(2)]
            self.assertEqual(first.result(), second.result())
        self.game.begin_fetch.assert_called_once()
        self.assertEqual(self.send("stop", "older-job")[1]["job_id"], "new-job")
        self.game.app.pause_player_name_fetch.assert_not_called()
        self.assertEqual(self.send("stop", "new-job")[0], 200)
        self.assertEqual(self.send("pause", "new-job")[0], 200)
        self.assertIs(self.store.setting(INFLIGHT)["stop_requested"], True)

    def test_stale_resume_after_stop_cannot_start_fresh_area(self):
        self.store.set_setting(LAST_FETCH, {"root": str(self.root), **self.game.options(), "character_id": "alice",
            "status": {"state":"stopped", "job_id":"old-paused"}})
        status, _ = self.send("start", "old-paused")
        self.assertIn(status, (200, 400, 409))
        self.game.begin_fetch.assert_not_called()
        self.assertIsNone(self.game.active)

    def test_current_resume_retains_selected_dialogues(self):
        self.store.set_setting(LAST_FETCH, {"root":str(self.root), **self.game.options(), "character_id":"alice",
            "status":{"state":"paused", "job_id":"current-paused"}, "resume_job":{"source_keys":["original.ogg"]}})
        self.assertEqual(self.send("start", "current-paused")[0], 200)
        self.assertEqual(self.game.begin_fetch.call_args.kwargs["source_keys"], ["original.ogg"])

    def test_archived_selection_from_another_tab_cannot_start(self):
        self.store.set_setting("lite_wow_removed_profiles_" + str(self.root), {"alice": {"name":"Alice"}})
        self.game.popup_player.return_value = (None, {"id":"beth", "values":{"name":"Beth"}})
        self.assertEqual(self.send("start", profile_id="alice")[0], 400)
        self.game.begin_fetch.assert_not_called()

    def test_switch_current_character_before_confirmation(self):
        self.game.popup_player.return_value = (None, {"id": "beth", "values": {"name": "Beth"}})
        self.assertEqual(self.send("start")[0], 400)
        self.game.begin_fetch.assert_not_called()

    def test_exclusive_database_lock_returns_json_then_recovers(self):
        self.store.db.execute("PRAGMA journal_mode=DELETE")
        self.store.db.execute("PRAGMA busy_timeout=0")
        key = "lite_wow_profiles_" + str(self.root)
        self.store.set_setting(key, {"alice": {"name":"Alice", "realm":"Test", "race":"Human", "class":"Mage"}})
        self.game.name_recording_counts = lambda: {}
        self.game.popup_player.return_value = None
        route = SETTINGS+"profile-archive"
        data = {"character_id":"alice", "removed":True}
        with closing(sqlite3.connect(self.root/"studio.sqlite")) as locked:
            locked.execute("BEGIN EXCLUSIVE")
            for verb in ("get", "post"):
                with self.subTest(verb=verb):
                    status, body = request(self.port, route, data=data if verb=="post" else None, headers=self.headers)
                    self.assertEqual(status, 503)
                    self.assertEqual(json.loads(body)["error_code"], "settings_busy")
            locked.rollback()
        self.assertEqual(json.loads(request(self.port, route)[1]), {})
        self.assertEqual(request(self.port, route, data=data, headers=self.headers)[0], 200)
        self.assertIn("alice", json.loads(request(self.port, route)[1]))


if __name__ == "__main__":
    unittest.main()
