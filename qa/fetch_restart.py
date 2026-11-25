"""Exercise interrupted fetch recovery without a game session or provider."""

from copy import deepcopy
import json
from pathlib import Path
from sqlite3 import OperationalError
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "source"))
from lite_card_transfer import CardTransfer, LIBRARY
from lite_game_ingress import GameIngress, INFLIGHT, INPUTS, LAST_FETCH
from lite_name_settings import PREFS
from lite_portable_render import NameCutUnavailable
from lite_return_review import ReturnNeedsReview
from name_fetch_jobs import NameFetchJobs
from spoken_player_names import SETTING, obtain_fresh, run


class Store:
    def __init__(self):
        self.settings = {}

    def setting(self, key, default=None):
        return deepcopy(self.settings.get(key, default))

    def set_setting(self, key, value):
        self.settings[key] = deepcopy(value)


class RestartTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = Store()
        self.record = {
            "root": str(self.root), "request_id": "100:10", "origin": "popup",
            "character": {"name": "Veylora"},
            "job": {"character_id": "owner", "provider_id": "fish-s21-free", "coverage": "route",
                    "allow_metered": False, "fetch_held_with_missing": True, "game_request_id": "100:10",
                    "source_keys": ["q:456:accept"]},
        }
        self.store.set_setting(INFLIGHT, self.record)
        self.ingress = GameIngress.__new__(GameIngress)
        self.ingress.app = SimpleNamespace(
            store=self.store, activity=Mock(),
            job=Mock(return_value={"status": "queued", "request_mode": "fetch", "character_id": "owner"}),
            pause_player_name_fetch=Mock(),
        )
        self.ingress.root = self.root
        self.ingress.addons = self.root / "Interface/AddOns"
        self.ingress.account = self.root / "WTF/Account"
        self.ingress.active = None
        self.ingress.progress_fetch = None
        self.ingress.resume_retry_at = 0
        self.ingress.resume_error = ""
        self.ingress.request_lock = Lock()
        self.ingress.cards = Mock()
        self.ingress.check_provider = Mock(return_value={"ok": True})
        self.ingress.submit_fetch = Mock(return_value={"id": "restored-job"})
        self.ingress.options = Mock(return_value={"provider_id": "fish-s21-free", "coverage": "route"})
        self.ingress.fetch_errors = Mock(return_value={})
        self.ingress.report_handoff = Mock()
        self.ingress.preparation_state = Mock(return_value={"state": "ready"})
        self.ingress.sync_game_roster = Mock()
        self.ingress.consume_legacy_requests = Mock()

    def status(self, owner="owner"):
        return self.ingress.popup_fetch({"action": "status", "character_id": owner})

    def test_roster_and_journal_locked_do_not_block_recovery(self):
        self.ingress.sync_game_roster.side_effect = PermissionError("Profiles.lua is locked")
        self.ingress.app.activity.append.side_effect = OSError("journal locked")
        self.ingress.tick()
        self.ingress.tick()
        self.ingress.submit_fetch.assert_called_once()
        self.assertEqual(self.ingress.active[0], "restored-job")

    def test_monitor_retries_database_busy_and_status_addon_file_lock(self):
        self.ingress.stop = Mock()
        self.ingress.stop.is_set.side_effect = [False, False, True]
        self.ingress.snapshot_lock = Lock()
        self.ingress.pending_snapshot = None
        self.ingress.finish_active = Mock(side_effect=[OperationalError("database is locked"), None])
        self.ingress.app.activity.append.side_effect = PermissionError("journal locked")
        with patch("lite_game_ingress.ensure_status_addon", side_effect=PermissionError("game file locked")):
            self.ingress.watch()
        self.assertEqual(self.ingress.finish_active.call_count, 2)
        self.ingress.submit_fetch.assert_called_once()

    def test_shared_watchers_survive_busy_settings_store(self):
        cards = CardTransfer.__new__(CardTransfer)
        cards.ingress, cards.store, cards.sync_key = self.ingress, Mock(), "fixture-sync"
        cards.check_playback, cards.synchronize = Mock(), Mock()
        for monitor in (cards.watch_playback, cards.watch):
            cards.store.setting.side_effect = [OperationalError("database is locked"), {"folder": "shared"}]
            stop = Mock()
            stop.is_set.side_effect = [False, False, True]
            stop.wait.return_value = False
            monitor(stop)
        cards.check_playback.assert_called_once()
        cards.synchronize.assert_called_once()

    def test_failed_name_reservation_or_submission_releases_preflight_ownership(self):
        self.store.set_setting(INFLIGHT, {})
        self.store.set_setting(INPUTS, {"plans": True})
        owner = self.record["character"]
        options = self.record["job"]

        def submit():
            self.ingress.begin_fetch(None, {"id": "owner"}, owner, options, "new-request", "fetch", popup=True)

        self.ingress.cards.reserve_fetch.side_effect = ValueError("shared library updating")
        with self.assertRaisesRegex(ValueError, "shared library updating"):
            submit()
        self.ingress.submit_fetch.assert_not_called()
        self.ingress.cards.finish_fetch_sync.assert_called_once()
        self.ingress.cards.reserve_fetch.side_effect = None
        with patch.object(self.store, "set_setting", side_effect=OperationalError("metadata is locked")):
            with self.assertRaisesRegex(OperationalError, "metadata is locked"):
                submit()
        self.ingress.submit_fetch.assert_not_called()
        self.ingress.submit_fetch.side_effect = ValueError("catalog changed")
        with self.assertRaisesRegex(ValueError, "catalog changed"):
            submit()
        self.assertEqual(self.store.setting(INFLIGHT), {})
        self.assertIsNone(self.ingress.active)
        self.assertEqual(self.ingress.cards.finish_fetch_sync.call_count, 3)

    def test_locked_db_cannot_leave_an_orphaned_share_reservation(self):
        self.store.set_setting(INFLIGHT, {})
        cards = CardTransfer.__new__(CardTransfer)
        cards.ingress, cards.store, cards.sync_key = self.ingress, self.store, "fixture-sync"
        cards.restore_before_fetch = Mock()
        device = "a" * 32
        self.store.set_setting(cards.sync_key, {"folder": str(self.root), "medium": "shared", "device": device})
        library = self.root / LIBRARY
        library.mkdir()
        with patch("lite_card_transfer.check_folder"):
            with patch.object(self.store, "set_setting", side_effect=OperationalError("database locked")):
                with self.assertRaises(OperationalError):
                    cards.reserve_fetch(self.record["character"])
            self.assertEqual(list(library.iterdir()), [])
            cards.reserve_fetch(self.record["character"])
            [claim] = library.glob("fetch-*")
            self.assertTrue((claim / device).is_file())
            cards.release_saved_fetch()
            self.assertEqual(list(library.iterdir()), [])
            claim.mkdir()
            peer = claim / ("b" * 32)
            peer.write_bytes(b"active")
            with self.assertRaisesRegex(ValueError, "Another computer"):
                cards.reserve_fetch(self.record["character"])
            self.assertEqual(self.store.setting(cards.sync_key + "_fetch", {}), {})
            self.assertTrue(peer.is_file())

    def command(self, action, job_id="100:10"):
        return self.ingress.popup_fetch({"action": action, "character_id": "owner", "job_id": job_id})

    def test_listener_reopens_and_schedules_persisted_batch_once(self):
        self.assertEqual(self.status()["state"], "queued")
        self.assertEqual(self.status("someone-else")["state"], "busy")
        self.ingress.tick()
        self.ingress.tick()
        restored = self.ingress.submit_fetch.call_args.args[0]
        self.ingress.submit_fetch.assert_called_once()
        for field, value in self.record["job"].items():
            self.assertEqual(restored[field], value)
        self.assertTrue(restored["_resume_interrupted_fetch"])
        self.assertEqual(self.status()["job_id"], "restored-job")
        self.assertEqual(self.store.setting(INFLIGHT), self.record)
        self.command("stop")
        self.ingress.app.pause_player_name_fetch.assert_called_once_with("restored-job")
        self.assertTrue(self.store.setting(INFLIGHT)["stop_requested"])

    def test_pause_stop_and_replacement_survive_restart(self):
        for field, status in (("pause_requested", "paused"), ("stop_requested", "stopped"),
                              ("replace_recording", "stopped")):
            with self.subTest(field=field):
                record = deepcopy(self.record)
                if field == "replace_recording":
                    record["job"][field] = {"slot": 1}
                else:
                    record[field] = True
                self.store.set_setting(INFLIGHT, record)
                self.ingress.tick()
                self.ingress.submit_fetch.assert_not_called()
                self.ingress.check_provider.assert_not_called()
                self.assertEqual(self.store.setting(LAST_FETCH)["status"]["state"], status)
                self.assertEqual(self.store.setting(INFLIGHT), {})

    def test_temporary_provider_failure_retries_without_clicking_resume(self):
        self.ingress.check_provider.side_effect = [{"ok": False, "message": "Service offline."}, {"ok": True}]
        with patch("lite_game_ingress.monotonic", side_effect=[100, 100, 110, 116]):
            self.ingress.tick()
            self.assertEqual(self.status()["detail"], "Waiting to resume fetch. Service offline.")
            self.ingress.tick()
            self.ingress.submit_fetch.assert_not_called()
            self.ingress.tick()
        self.ingress.submit_fetch.assert_called_once()

    def test_pending_stop_and_stale_job(self):
        self.ingress.resume_retry_at = float("inf")
        self.assertEqual(self.command("stop", "older-id")["state"], "queued")
        self.assertEqual(self.command("stop")["state"], "stopped")
        self.ingress.tick()
        self.ingress.submit_fetch.assert_not_called()

    def poll_playback_once(self, config, publish_error=None, folder_error=None):
        cards = CardTransfer.__new__(CardTransfer)
        cards.ingress, cards.store, cards.sync_key = self.ingress, self.store, "fixture-sync"
        self.store.set_setting(cards.sync_key, config)
        cards.publish_playback = Mock(side_effect=publish_error)
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch("lite_card_transfer.check_folder", side_effect=folder_error):
            cards.watch_playback(stop)

    def test_local_playback_probe_or_stale_share_check_cannot_pause_fetch(self):
        self.ingress.tick()
        active = self.ingress.active
        for config in ({}, {"folder": "share"}):
            self.poll_playback_once(config, publish_error=PermissionError("probe locked"))
        def reconfigured(_):
            self.store.set_setting("fixture-sync", {})
            raise OSError("old share unavailable")
        self.poll_playback_once({"folder": "share"}, folder_error=reconfigured)
        def replaced(_):
            self.ingress.active = ("new-job", *active[1:])
            raise OSError("prior fetch share unavailable")
        self.poll_playback_once({"folder": "share"}, folder_error=replaced)
        self.ingress.app.pause_player_name_fetch.assert_not_called()
        self.assertNotIn("pause_requested", self.store.setting(INFLIGHT))

    def test_active_share_outage_pauses_and_preserves_its_reason(self):
        self.ingress.tick()
        for _ in range(2):
            self.poll_playback_once({"folder": "share"}, publish_error=OSError("probe locked"),
                                    folder_error=OSError("share offline"))
        self.ingress.app.pause_player_name_fetch.assert_called_once_with("restored-job")
        reason = self.store.setting(INFLIGHT)["pause_reason"]
        self.assertIn("shared name library", reason)
        self.assertEqual(self.command("status", "restored-job")["detail"], reason)
        self.ingress.active = None
        self.ingress.tick()
        recovered = self.store.setting(LAST_FETCH)["status"]
        self.assertEqual((recovered["state"], recovered["detail"]), ("paused", reason))


class RequestReceiptTests(unittest.TestCase):
    def test_interrupted_provider_post_is_never_reissued_on_resume(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stock = root / "stock.ogg"
            stock.write_bytes(b"fixture")
            request_id = "a" * 64
            folder = root / request_id
            folder.mkdir()
            receipt = folder / "receipt.json"
            saved = {"state": "submitting", "request_sha256": request_id}
            receipt.write_text(json.dumps(saved))
            profile = SimpleNamespace(engine="azure", request=Mock(return_value={}),
                                      identity=Mock(return_value=request_id), obtain=Mock())
            with self.assertRaises(ReturnNeedsReview):
                obtain_fresh(SimpleNamespace(store_managed=True),
                             {"source_id": "q:456:accept", "npc_id": 2079, "stock_text": "Greetings, Adventurer."},
                             "Greetings, Veylora.", profile, stock, root, root,
                             False, True, Mock(), Mock(), cached_only=False, retry_held=True,
                             retry_missing=True, replace_saved=True, resume_interrupted_fetch=True)
            profile.obtain.assert_not_called()
            self.assertEqual(json.loads(receipt.read_text()), saved)


class WorkerExitTests(unittest.TestCase):
    def test_worker_shutdown_signals_active_work_and_preserves_its_result_with_locked_logs(self):
        journal = Mock()
        journal.append.side_effect = PermissionError("Journal is locked")
        jobs = {}
        pool = NameFetchJobs(jobs, Lock(), journal)
        started, finish = Event(), Event()
        def fetch(_request, _report, *, should_stop, **_callbacks):
            started.set()
            self.assertTrue(finish.wait(5))
            return {"paused": should_stop(), "placed_dialogues": 1}
        request = {"submit": True, "character_id": "owner"}
        try:
            first = pool.submit(request, fetch)
            self.assertTrue(started.wait(5))
            self.assertEqual(pool.submit(request, fetch)["id"], first["id"])
            queued_work = Mock()
            cancelled = Event()
            submit = pool.pool.submit
            def watch_queued(*args):
                future = submit(*args)
                future.add_done_callback(lambda f: cancelled.set() if f.cancelled() else None)
                return future
            with patch.object(pool.pool, "submit", side_effect=watch_queued):
                pool.submit({**request, "character_id": "other"}, queued_work)
            close = Thread(target=pool.close)
            close.start()
            self.assertTrue(pool.stops[first["id"]].wait(5))
            self.assertTrue(cancelled.wait(5))
            with self.assertRaisesRegex(ValueError, "shutting down"):
                pool.submit(request, fetch)
            finish.set()
            close.join(5)
            self.assertFalse(close.is_alive())
            self.assertEqual(jobs[first["id"]]["status"], "complete")
            self.assertEqual(jobs[first["id"]]["result"], {"paused": True, "placed_dialogues": 1})
            queued_work.assert_not_called()
        finally:
            finish.set()
            pool.close()


class DialogueFailureTests(unittest.TestCase):
    def test_join_failure_does_not_pause_an_old_installation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            addons = root / "AddOns"
            for addon in ("Spoken", "Spoken_Quests"):
                (addons / addon).mkdir(parents=True)
                (addons / addon / (addon + ".toc")).touch()
            store = Store()
            store.set_setting(PREFS, {"stop_on_dialogue_error": True})
            store.set_setting(SETTING, {"plans": {"sha256": "fixture"}})
            store.set_setting("characters", [{"id": "owner", "values": {"name": "Veylora", "race": "Human"}}])
            dialogues = [
                {"source_id": f"q:{ident}:accept", "npc_id": ident, "npc": f"NPC {ident}",
                 "races": ["human"], "area": "northshire", "stock_path": f"{ident}.ogg"}
                for ident in (1, 2)
            ]
            app = SimpleNamespace(store=store, folder=root, connections=Mock())
            app.connections.profile.return_value = {"engine": "azure"}
            activity = Mock()
            with (
                patch("spoken_player_names.imported", side_effect=[({"dialogues": dialogues}, {}), ({}, {})]),
                patch("spoken_player_names.carry_compatible"),
                patch("spoken_player_names.publish_snapshot"),
                patch("lite_expanded_name_stage.checked_binding", side_effect=[
                    NameCutUnavailable("No phonetic cut fits."),
                    ValueError("The second dialogue reached processing."),
                ]),
            ):
                result = run(app, {"provider_id": "azure", "character_id": "owner", "submit": True,
                                   "addons_folder": str(addons), "stop_on_dialogue_error": True},
                             Mock(), on_activity=activity)
            self.assertFalse(result["paused"])
            self.assertEqual([row["reason"] for row in result["statuses"]],
                             ["No phonetic cut fits.", "The second dialogue reached processing."])
            self.assertEqual([call.args[0]["dialogue_number"] for call in activity.call_args_list], [1, 2])
            self.assertNotIn("stop_on_dialogue_error", store.setting(PREFS))


if __name__ == "__main__":
    unittest.main()
