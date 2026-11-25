"""Verify durable, scoped timings through the desktop fetch API."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
from lite_fetch_timing import history_key, save_pass, timing_report
from lite_game_ingress import GameIngress, INFLIGHT, LAST_FETCH, OPTIONS
from settings_store import SettingsStore


class FetchTimingTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SettingsStore(directory.name)
        self.addCleanup(self.store.close)
        self.ingress = GameIngress.__new__(GameIngress)
        self.ingress.app = SimpleNamespace(store=self.store)
        self.ingress.root = Path(directory.name)
        self.ingress.active = None
        self.ingress.request_lock = Lock()
        self.ingress.fetch_errors = Mock(return_value={})
        self.options = {"provider_id": "fish", "coverage": "starting-area"}
        self.store.set_setting(OPTIONS, self.options)
        self.scope = {"character_id": "player", **self.options}
        self.key = history_key(self.ingress.root, **self.scope)

    def record(self, request, duration, finish):
        return {"request_id": request, "root": str(self.ingress.root), **self.scope,
                "accepted_at_ns": (finish - duration) * 1_000_000_000,
                "finished_at_ns": finish * 1_000_000_000,
                "status": {"state": "complete", "elapsed_seconds": duration, "ready": 1, "total": 1}}

    def test_previous_build_and_out_of_order_history(self):
        recent, older = self.record("two", 7, 40), self.record("one", 5, 20)
        self.store.set_setting(LAST_FETCH, recent)
        request = {"action": "status", **self.scope}
        self.assertEqual(self.ingress.popup_fetch(request)["timing"]["total_seconds"], 7)
        save_pass(self.store, recent)
        save_pass(self.store, older)
        save_pass(self.store, older)
        report = self.ingress.popup_fetch(request)["timing"]
        self.assertEqual(report["total_seconds"], 12)
        self.assertEqual([row["request_id"] for row in report["passes"]], ["one", "two"])
        self.assertEqual(report["passes"][0]["dialogue_count"], 0)
        for isolated in ({"character_id": "another"}, {"provider_id": "local"}, {"coverage": "route"}):
            other = history_key(self.ingress.root, **{**self.scope, **isolated})
            self.assertEqual(timing_report(self.store, other, recent), {"passes": [], "total_seconds": 0})

    def test_terminal_poll_records_durations_and_only_loads_dialogues_on_demand(self):
        self.ingress.active = ("worker", "request", "fetch", None, "player", {"name": "Player"}, None)
        self.ingress.store_managed = False
        self.ingress.cards = Mock()
        self.ingress.report_handoff = Mock()
        self.ingress.remember_fetch_errors = Mock()
        self.store.set_setting(INFLIGHT, {"root": str(self.ingress.root), "request_id": "request", "origin": "popup",
                                          "accepted_at_ns": 10_000_000_000, "job": self.scope})
        row = {"source_id": "q:1", "source_key": "q:1:progress", "state": "staged", "npc": "Brother Paxton",
               "stage": "Quest progress", "elapsed_seconds": 0, "already_ready": True, "private_detail": "excluded"}
        self.ingress.app.job = lambda ident: {
            "id": ident, "character_id": "player", "request_mode": "fetch", "status": "complete", "created": 10,
            "finished_at_ns": 15_000_000_000, "result": {"elapsed_seconds": 4.98, "statuses": [row],
                "paused": False, "placement_errors": [], "placed_dialogues": 1}}
        result = self.ingress.popup_fetch({"action": "status", **self.scope})
        self.assertEqual(result["timing"]["total_seconds"], 5)
        self.assertEqual(result["timing"]["passes"][0]["dialogue_count"], 1)
        self.assertNotIn("dialogues", result["timing"]["passes"][0])
        query = {"action": "timing", "request_id": "request", **self.scope}
        self.assertEqual(self.ingress.popup_fetch(query)["dialogues"],
                         [{key: value for key, value in row.items() if key != "private_detail"}])
        self.assertEqual(self.ingress.popup_fetch(query)["npc_times"], [{"npc": "Brother Paxton", "elapsed_seconds": 0}])
        self.assertFalse(self.ingress.popup_fetch({**query, "character_id": "other"})["dialogues"])
        with self.assertRaisesRegex(ValueError, "selection changed"):
            self.ingress.popup_fetch({**query, "coverage": "all"})
        self.assertEqual(self.ingress.popup_fetch({"action": "status", **self.scope})["timing"], result["timing"])

    def test_current_speaker_and_history_exclude_other_speakers_and_prior_passes(self):
        save_pass(self.store, self.record("previous", 90, 100))
        self.ingress.active = ("worker", "request", "fetch", None, "player", {"name": "Player"}, None)
        self.store.set_setting(INFLIGHT, {"request_id": "request", "accepted_at_ns": 200_000_000_000, "job": self.scope})
        active = {"npc": "Zureetha", "npc_started_at": 101, "npc_sampled_at": 102}
        job = {"character_id": "player", "request_mode": "fetch", "status": "running",
               "created": 201, "name_activity": active}
        self.ingress.app.job = lambda ident: job
        with patch("lite_game_ingress.time", return_value=212), patch("lite_game_ingress.perf_counter", return_value=104):
            live = self.ingress._popup_fetch_status("worker", "player")
            self.assertEqual(live["pass_elapsed_seconds"], 12)
            self.assertEqual(live["npc_time"], {"npc": "Zureetha", "elapsed_seconds": 3})
        rows = [{"npc": name, "npc_id": ident, "elapsed_seconds": seconds}
                for name, ident, seconds in (("Kaltunk", 1, 2), ("Zureetha", 2, 3), ("Kaltunk", 1, 4))]
        save_pass(self.store, self.record("current", 12, 212), rows)
        query = {"action": "timing", "request_id": "current", **self.scope}
        self.assertEqual(self.ingress.popup_fetch(query)["npc_times"],
                         [{"npc": "Kaltunk", "elapsed_seconds": 6}, {"npc": "Zureetha", "elapsed_seconds": 3}])
        self.assertEqual(timing_report(self.store, self.key, {})["passes"][-1]["elapsed_seconds"], 12)

    def test_damaged_history_does_not_prevent_a_new_result(self):
        earlier = self.record("first", 3, 11)
        latest = self.record("second", 5, 20)
        save_pass(self.store, earlier)
        missing_duration = {"request_id": "damaged", "state": "complete"}
        self.store.set_setting(self.key, [*self.store.setting(self.key), None, missing_duration])
        save_pass(self.store, latest)
        report = self.ingress.popup_fetch({"action": "status", **self.scope})["timing"]
        self.assertTrue(report["incomplete_history"])
        self.assertEqual(report["total_seconds"], 8)
        self.assertEqual([entry["request_id"] for entry in report["passes"]], ["first", "second"])
        self.assertIn(missing_duration, self.store.setting(self.key))

    def test_truncated_optional_history_is_preserved_while_new_fetches_are_recorded(self):
        damaged_text = '[{"request_id":"old"'
        with self.store.db:
            self.store.db.execute("INSERT INTO settings VALUES (?, ?)", (self.key, damaged_text))
        save_pass(self.store, self.record("new", 9, 20))
        report = self.ingress.popup_fetch({"action": "status", **self.scope})["timing"]
        self.assertTrue(report["incomplete_history"])
        self.assertEqual(report["total_seconds"], 9)
        self.assertIn(damaged_text, self.store.setting(self.key))

    def test_stopped_pass_and_missing_worker_measurement(self):
        saved = self.record("pause", 2, 10)
        saved["status"] = {"state": "paused"}
        save_pass(self.store, saved)
        save_pass(self.store, {**saved, "status": {"state": "stopped"}})
        incomplete = {**saved, "request_id": "interrupted", "finished_at_ns": None}
        save_pass(self.store, incomplete)
        summary = timing_report(self.store, self.key, incomplete)
        self.assertEqual(summary["total_seconds"], 2)
        self.assertEqual([row["state"] for row in summary["passes"]], ["stopped"])


if __name__ == "__main__":
    unittest.main()
