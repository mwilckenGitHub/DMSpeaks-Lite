"""Check missing original recordings with an isolated catalog and profile."""
from pathlib import Path
import sys
from threading import Lock
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_name_retakes
from lite_fetch_timing import save_pass
from lite_game_ingress import GameIngress, INPUTS, LAST_FETCH, completed_fetch
from lite_portable_references import LEGACY_MISSING_SPOKEN, OriginalSpokenNotCatalogued, require_hosted_playback_base
import spoken_player_names as names


class SourceAvailability(unittest.TestCase):
    def setUp(self):
        fixture = self.fixture = fetch_name_retakes.FitRetakes()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.checked.return_value = None
        self.native = dict(fixture.row, key="5847-complete", npc="Claire Willower", source_id="q:5847:complete",
                           native_source={"source_contract_sha256": "a" * 64})
        del self.native["stock_path"]
        self.options = {"provider_id": "fish-s21-free", "coverage": "starting"}
        self.job = {"character_id": "owner", **self.options}
        self.ingress = GameIngress.__new__(GameIngress)
        self.ingress.app = fixture.app
        self.ingress.root = fixture.root
        self.ingress.options = lambda: self.options
        self.ingress.request_lock = Lock()
        self.ingress.popup_selection = Mock(return_value=(None, {"id": "owner"}, self.options))

    def test_native_block_continues_and_a_verified_saved_name_still_counts(self):
        fixture = self.fixture
        rows = [self.native, fixture.row]
        def catalog(app, part, **kw):
            return ({"dialogues": rows}, {}) if part == "plans" else ({}, {})
        def preflight(app, row, state, *, verify_audio):
            return {}, {"source_id": row["source_id"]}, {}, {}
        cached = {fixture.row["source_id"]}
        with (patch.object(names, "imported", side_effect=catalog),
              patch("lite_paced_name.preflight_name_base", side_effect=preflight),
              patch("lite_store_playback.name_present", side_effect=lambda addons, manifest, *args, **kw: manifest["source_id"] in cached),
              patch.object(names, "obtain_fresh") as provider):
            result = fixture.fetch()
            blocked, ready = result["statuses"]
            self.assertEqual((blocked["state"], ready["state"]), ("source_unavailable", "staged"))
            self.assertIn("Claire Willower (quest 5847)", blocked["reason"])
            self.assertEqual(fixture.retries(), [])
            self.assertEqual(result["areas"][0]["source_unavailable"], 1)
            status = completed_fetch(result)
            self.assertEqual((status["ready"], status["total"], status["source_unavailable"], status["retryable_returns"]),
                             (1, 2, 1, 0))
            self.ingress.remember_fetch_errors(self.job, result)
            review = self.ingress.popup_fetch({"action": "review", "character_id": "owner"})
            self.assertEqual(review["errors"], [])
            self.assertEqual([item["source_key"] for item in review["source_unavailable"]], [names.source_key(self.native)])
            self.assertEqual(self.ingress.fetch_errors("owner", self.options), {})
            cached.add(self.native["source_id"])
            recovered = fixture.fetch()
            self.assertEqual(recovered["placed_dialogues"], 2)
            self.ingress.remember_fetch_errors(self.job, recovered)
            self.assertEqual(self.ingress.fetch_issues("owner", self.options), {})
            provider.assert_not_called()

    def test_mixed_legacy_timing_only_updates_rows_confirmed_for_that_pass(self):
        key = names.source_key(self.native)
        missing = {"state": "held", "source_key": key, "source_id": self.native["source_id"],
                   "npc": self.native["npc"], "reason": LEGACY_MISSING_SPOKEN}
        stale = "native:" + "b" * 64
        local = "Install the matching signed Spoken recording in the selected AddOns folder."
        store = self.fixture.app.store
        store.set_setting(self.ingress.fetch_errors_key("owner", self.options),
                          {key: missing, stale: {**missing, "source_key": stale}, "local.ogg": {"reason": local}})
        saved = {**self.job, "root": str(self.ingress.root), "request_id": "test", "status": {
            "state": "unavailable", "ready": 14, "total": 16, "elapsed_seconds": 8,
            "remaining_reason": local, "fetch_scope": "area"}}
        rows = [{"source_key": source, "state": state, "elapsed_seconds": 1}
                for source, state in [(key, "held"), ("local.ogg", "held"), (stale, "staged")]]
        save_pass(store, saved, rows)
        older = {**saved, "request_id": "older"}
        save_pass(store, older, rows)
        store.set_setting(LAST_FETCH, saved)
        result = self.ingress.last_fetch_status("owner")
        self.assertEqual((result["ready"], result["total"], result.get("source_unavailable", 0)), (14, 16, 0))
        self.assertEqual(result["remaining_reason"], local)
        query = {"action": "timing", **self.job}
        latest = self.ingress.popup_fetch({**query, "request_id": "test"})["dialogues"]
        self.assertEqual([row["state"] for row in latest], ["source_unavailable", "held", "staged"])
        history = self.ingress.popup_fetch({**query, "request_id": "older"})["dialogues"]
        self.assertEqual(history, rows)

    def test_pre_upgrade_held_record_is_consistent_across_status_review_and_timing(self):
        key = names.source_key(self.native)
        item = {"source_key": key, "source_id": self.native["source_id"], "npc": self.native["npc"],
                "state": "held", "reason": LEGACY_MISSING_SPOKEN}
        store = self.fixture.app.store
        store.set_setting(self.ingress.fetch_errors_key("owner", self.options), {key: item})
        saved = {**self.job, "root": str(self.ingress.root), "request_id": "deathknell", "status": {
            "state": "unavailable", "ready": 15, "total": 16, "elapsed_seconds": 3.199,
            "fetch_scope": "area", "retryable_returns": 0, "remaining_reason": LEGACY_MISSING_SPOKEN,
            "detail": "Reload WoW to use your new audio. " + LEGACY_MISSING_SPOKEN}}
        rows = [{"source_key": key, "state": "held", "elapsed_seconds": 0.5}]
        save_pass(store, saved, rows)
        store.set_setting(LAST_FETCH, saved)
        self.ingress.active = None
        self.ingress.recovering_fetch = lambda: None
        status = self.ingress.popup_fetch({"action": "status", **self.job})
        self.assertEqual((status["ready"], status["total"], status["review_count"]), (15, 16, 1))
        self.assertEqual(status.get("source_unavailable", 0), 0)
        self.assertIn("Claire Willower (quest 5847)", status["detail"])
        self.assertIn("default voice reference", status["detail"])
        review = self.ingress.popup_fetch({"action": "review", **self.job})
        self.assertEqual(review["source_unavailable"], [])
        self.assertEqual(review["errors"], [{**item, "state": "source_recheck", "reason": status["remaining_reason"]}])
        timing = self.ingress.popup_fetch({"action": "timing", **self.job, "request_id": "deathknell"})
        self.assertEqual(timing["dialogues"], [{**rows[0], "state": "source_unavailable"}])
        self.assertEqual(status["timing"]["total_seconds"], 3.199)

    def test_only_native_legacy_error_has_evidence_of_a_missing_catalog_source(self):
        key = names.source_key(self.native)
        stock = "missing.ogg"
        item = {"npc": self.native["npc"], "source_id": self.native["source_id"], "reason": LEGACY_MISSING_SPOKEN}
        store = self.fixture.app.store
        store.set_setting(self.ingress.fetch_errors_key("owner", self.options), {key: item, stock: item})
        store.set_setting(LAST_FETCH, {**self.job, "root": str(self.ingress.root), "status": {
            "remaining_reason": LEGACY_MISSING_SPOKEN, "fetch_scope": "area", "state": "unavailable",
            "ready": 15, "total": 16}})
        status = self.ingress.last_fetch_status("owner")
        self.assertEqual((status["ready"], status["total"], status.get("source_unavailable", 0)), (15, 16, 0))
        self.assertIn("quest 5847", status["remaining_reason"])
        self.assertEqual(set(self.ingress.fetch_errors("owner", self.options)), {stock, key})
        local_error = {"state": "held", "reason": "Install the matching signed Spoken recording in the selected AddOns folder."}
        store.set_setting(self.ingress.fetch_errors_key("owner", self.options), {key: item, stock: local_error})
        self.assertEqual(self.ingress.fetch_errors("owner", self.options)[stock], local_error)
        with self.assertRaises(OriginalSpokenNotCatalogued):
            require_hosted_playback_base(self.native)
        with self.assertRaises(ValueError) as error:
            require_hosted_playback_base({})
        self.assertNotIsInstance(error.exception, OriginalSpokenNotCatalogued)

    def test_old_blocked_state_allows_start_and_targeted_retry_until_rechecked(self):
        key = names.source_key(self.native)
        issue = {"source_key": key, "npc": "Claire Willower", "source_id": "q:5847:complete",
                 "reason": "The catalog has no original Spoken recording linked for Claire Willower (quest 5847).",
                 "state": "source_unavailable"}
        store = self.fixture.app.store
        store.set_setting(self.ingress.fetch_errors_key("owner", self.options), {key: issue})
        self.options["allow_metered"] = False
        self.ingress.active = None
        self.ingress.tick = Mock()
        self.ingress.begin_fetch = Mock(return_value="job")
        self.ingress.popup_fetch_status = Mock(return_value={"state": "queued"})
        self.ingress.popup_selection.return_value = (None, {"id": "owner", "values": {}}, self.options)
        query = {"action": "start", "character_id": "owner", "allow_metered": False}
        with patch("lite_game_ingress.remember_character"):
            self.ingress.popup_fetch(query)
            self.assertIsNone(self.ingress.begin_fetch.call_args.kwargs["source_keys"])
            self.ingress.popup_fetch({**query, "source_keys": [key]})
            self.assertEqual(self.ingress.begin_fetch.call_args.kwargs["source_keys"], [key])
        store.set_setting(LAST_FETCH, {**self.job, "root": str(self.ingress.root), "request_id": "old", "status": {
            "state": "unavailable", "fetch_scope": "area", "source_unavailable": 1, "ready": 15, "total": 16,
            "remaining_reason": issue["reason"], "detail": issue["reason"]}})
        status = self.ingress.last_fetch_status("owner")
        self.assertEqual(status["source_unavailable"], 0)
        self.assertIn("default voice reference", status["remaining_reason"])

        self.ingress.remember_fetch_errors(self.job, {"statuses": [{**issue, "reason": "No matching voice reference."}]})
        self.assertNotIn(key, self.ingress.fetch_errors("owner", self.options))
        with self.assertRaisesRegex(ValueError, "current fetch error list"):
            self.ingress.popup_fetch({**query, "source_keys": [key]})
        store.set_setting(INPUTS, {"plans": {"sha256": "b" * 64}})
        self.assertEqual(self.ingress.fetch_errors("owner", self.options)[key]["state"], "source_recheck")
        self.ingress.remember_fetch_errors(self.job, {"statuses": [{"source_key": key, "state": "staged"}]})
        self.assertEqual(self.ingress.fetch_issues("owner", self.options), {})


if __name__ == "__main__":
    unittest.main()
