"""Exercise desktop coverage and HUD presentation using the same job snapshot."""

from pathlib import Path
import sys
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "source"))
from lite_game_ingress import GameIngress, completed_fetch


class ProgressStatus(unittest.TestCase):
    def setUp(self):
        self.progress = {
            "npc": {"id": 3143, "label": "Gornek", "ready": 2, "total": 5},
            "zone": {"id": "valley-of-trials", "label": "Valley of Trials", "ready": 6, "total": 17},
            "overall": {"ready": 14, "total": 2207},
        }
        self.job = {
            "id": "fetch", "request_mode": "fetch", "character_id": "player", "created": 10,
            "status": "running", "name_activity": {"npc": "Gornek", "dialogue_number": 1,
                "total": 1, "coverage_progress": self.progress, "instance_progress": 75,
                "retake_attempt": 2, "retake_total": 3},
        }
        self.ingress = GameIngress.__new__(GameIngress)
        store = Mock()
        store.setting.side_effect = lambda key, default=None: default
        self.ingress.app = SimpleNamespace(job=lambda ident: self.job, store=store)
        self.ingress.active = None
        self.ingress.root = Path("client")
        self.ingress.coverage_lock = Lock()
        self.ingress.progress_fetch = ("fetch", None, None, None, "player", {"name": "Player"}, None)
        self.ingress.options = lambda: {}
        self.ingress.fetch_errors = lambda *args: {}

    def test_selected_fetch_carries_broader_counts_and_attempt(self):
        status = self.ingress.popup_fetch_status("fetch", "player")
        self.assertEqual((status["total"], status["coverage_progress"]), (1, self.progress))
        self.assertEqual((status["retake_attempt"], status["retake_total"]), (2, 3))
        hud = self.ingress.progress_display()
        self.assertEqual((hud["title"], hud["count"]), ("Valley of Trials", "6/17 ready"))
        self.assertAlmostEqual(hud["percent"], 100 * 6 / 17)
        self.assertIn("Gornek", hud["detail"])

    def test_quick_check_returns_and_persists_same_snapshot(self):
        self.job.update(status="complete", request_mode="coverage", coverage_scope=("fish", "starting-area", None),
                        finished_at_ns=20_000_000_000, result={"coverage_count": {"ready": 6, "total": 17},
                                                              "coverage_progress": self.progress})
        result = self.ingress.coverage_status("fetch")
        saved = self.ingress.app.store.set_setting.call_args.args[1]
        self.assertEqual(saved["coverage_progress"], {**self.progress, "checked_at": 20})
        self.assertEqual(result["coverage_progress"], saved["coverage_progress"])
        self.assertEqual(result["checked_at"], saved["checked_at"])

    def test_partial_finish_explains_failure_and_preserves_counts(self):
        reason = "The provider returned an empty recording."
        self.job.update(status="complete", finished_at_ns=20_000_000_000, result={
            "statuses": [{"state": "staged"}, {"state": "held", "reason": reason}],
            "placed_dialogues": 1, "paused": False, "placement_errors": [],
            "coverage_progress": self.progress})
        result = self.ingress.popup_fetch_status("fetch", "player")
        self.assertEqual(result["remaining_reason"], reason)
        self.assertEqual(result["coverage_progress"], {**self.progress, "checked_at": 20})
        self.assertEqual(result["total"], 2)

    def test_fresher_matching_area_measurement_supersedes_only_finished_area_work(self):
        options = {"provider_id": "fish", "coverage": "starting-area"}
        self.ingress.options = lambda: options
        historical = {"state": "unavailable", "ready": 14, "total": 17, "fetch_scope": "area",
                      "remaining_reason": "The recording still needs a measured cut."}
        saved = {"root": str(self.ingress.root), "character_id": "player", **options,
                 "finished_at_ns": 20_000_000_000, "status": historical}
        checked = {"ready": 17, "total": 17, "checked_at": 21}
        from lite_game_ingress import LAST_FETCH
        coverage_key = self.ingress.coverage_key("player", "fish", "starting-area")
        records = {LAST_FETCH: saved, coverage_key: checked}
        self.ingress.app.store.setting.side_effect = records.get
        result = self.ingress.last_fetch_status("player")
        self.assertEqual(result, {**historical, "newer_area_coverage": checked})
        self.assertEqual(saved["status"], historical)
        for changes in ({"fetch_scope": "selected"}, {"fetch_scope": "replacement"}, {"fetch_scope": None},
                        {"state": "paused"}, {"state": "running"}, {"state": "stopped"}):
            saved["status"] = {**historical, **changes}
            self.assertNotIn("newer_area_coverage", self.ingress.last_fetch_status("player"))
        saved["status"] = historical
        for stamp in (19, 20):
            checked["checked_at"] = stamp
            self.assertNotIn("newer_area_coverage", self.ingress.last_fetch_status("player"))
        checked["checked_at"] = 21
        checked["ready"] = 14
        self.assertNotIn("newer_area_coverage", self.ingress.last_fetch_status("player"))
        checked["ready"] = 17
        saved["finished_at_ns"] = None
        self.assertNotIn("newer_area_coverage", self.ingress.last_fetch_status("player"))
        self.assertEqual(self.ingress.last_fetch_status("other"), {"state": "idle"})
        options["coverage"] = "all"
        self.assertEqual(self.ingress.last_fetch_status("player"), {"state": "idle"})


if __name__ == "__main__":
    unittest.main()
