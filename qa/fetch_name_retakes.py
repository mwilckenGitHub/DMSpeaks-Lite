"""Exercise bounded fit-failure retakes with synthetic local inputs."""
from contextlib import ExitStack
from base64 import b64encode
from http.client import IncompleteRead
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "source"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import spoken_player_names as names
from fetch_restart import Store
import fetch_provider_cooldown


class ContinuingNameContext(unittest.TestCase):
    def setUp(self):
        self.text = "Finally, you are of age, adventurer... of age to battle. More later."
        lo, hi = self.text.index("adventurer"), self.text.index(" of age to battle.")
        self.row = {"stock_text": self.text, "context": [0, hi], "slot": [lo, lo + len("adventurer")],
                    "leading_word": {"source_text": "age"}, "request_context": self.text[:hi]}

    def test_continuation_keeps_signed_name_and_measured_neighbor(self):
        from lite_portable_render import continuing_name_context, name_text
        result = continuing_name_context(self.row)
        self.assertEqual(result["slot"], self.row["slot"])
        self.assertEqual(self.row["context"][0], 0)
        self.assertEqual(result["request_context"], "of age, adventurer... of age to battle.")
        transcript, (lo, hi) = name_text(result, "Anora")
        self.assertEqual(transcript, "of age, Anora... of age to battle.")
        self.assertEqual(transcript[lo:hi], "Anora")

    def test_authored_custom_variant_and_multiple_names_keep_original_context(self):
        from lite_portable_render import continuing_name_context
        for row in ({**self.row, "request_context": "At last, you are of age, adventurer..."},
                    {**self.row, "name_slots": [self.row["slot"], self.row["slot"]]}):
            with self.subTest(row=row):
                self.assertIs(continuing_name_context(row), row)

    def test_finished_sentence_and_no_following_text_keep_original_context(self):
        from lite_portable_render import continuing_name_context
        text = self.text.replace("...", ".")
        stop = text.index(".") + 1
        for row in ({**self.row, "stock_text": text, "context": [0, stop], "request_context": text[:stop]},
                    {**self.row, "stock_text": self.row["request_context"]}):
            with self.subTest(row=row):
                self.assertIs(continuing_name_context(row), row)


class DetachedProviderName(unittest.TestCase):
    rate = 44100

    def setUp(self):
        import numpy as np
        self.pcm = np.zeros(2 * self.rate, dtype=np.float32)
        t = np.arange(self.rate // 2) / self.rate
        self.voice = .13 * np.sin(2 * np.pi * 165 * t) + .05 * np.sin(2 * np.pi * 330 * t)
        self.pcm[:len(t)] = self.voice
        self.pcm[self.rate:self.rate + len(t)] = self.voice
        self.raw = {"sha256": "a" * 64, "sample_count": len(self.pcm), "sample_rate": self.rate}
        self.words = [{"word": "Mira", "start": 0, "end": 1.5},
                      {"word": "go", "start": 1.6, "end": 1.85}]

    def check(self, *, digest=None, measured_end=.56):
        from lite_portable_render import review_provider_name_timing
        observation = {**self.raw, "words": self.words, "audio_sha256": self.raw["sha256"]}
        timing = {"format": "dmspeaks-word-timing-evidence", "bound_audio_sha256": digest or self.raw["sha256"],
                  "observations": {"generation": observation}}
        return review_provider_name_timing(self.raw, timing, "Mira, go.", [0, 4],
            {"name_samples": [0, round(measured_end * self.rate)]}, self.pcm, "b" * 64)

    def test_separate_voiced_passage_inside_name_retries_the_corresponding_request(self):
        with self.assertRaises(names.NameCutUnavailable) as failed:
            self.check()
        self.assertEqual(failed.exception.request_id, "b" * 64)

    def test_paced_choice_must_retain_the_complete_name_and_exclude_the_extra_word(self):
        from lite_paced_name import cuts_clear_of_reported_fallback
        name = {"name_samples": [40, 80], "paced_samples": [[10, 30]]}
        overlap = (0, None, [20, 81], None)
        short = (0, None, [30, 79], None)
        clean = (0, None, [30, 80], None)
        with self.assertRaises(names.NameCutUnavailable) as failed:
            cuts_clear_of_reported_fallback([overlap, short], name, "c" * 64)
        self.assertEqual(failed.exception.request_id, "c" * 64)
        self.assertEqual(cuts_clear_of_reported_fallback([overlap, clean, short], name, "c" * 64), [clean])

    def test_overlapping_timestamp_cannot_assign_next_word_to_name(self):
        self.words[1]["start"] = .9
        self.assertIsNotNone(self.check()[0])

    def test_mismatched_audio_short_offset_quiet_or_unvoiced_tail(self):
        import numpy as np
        self.assertEqual(self.check(digest="c" * 64), (None, []))
        self.assertIsNotNone(self.check(measured_end=1.4)[0])
        self.pcm[self.rate:] = 0
        self.assertIsNotNone(self.check()[0])
        self.pcm[self.rate:self.rate + len(self.voice)] = np.random.default_rng(12).normal(0, .05, len(self.voice))
        self.assertIsNotNone(self.check()[0])

    def test_sustained_vowel_is_not_a_second_utterance(self):
        self.pcm[:len(self.voice) * 3] = list(self.voice) * 3
        self.assertIsNotNone(self.check()[0])


class FitRetakes(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        addons = self.root / "AddOns"
        for addon in ("Spoken", "Spoken_Quests"):
            (addons / addon).mkdir(parents=True)
            (addons / addon / (addon + ".toc")).touch()
        self.app = SimpleNamespace(store=Store(), folder=self.root, connections=Mock(), store_managed=True)
        self.app.connections.profile.return_value = {"engine": "fish"}
        self.app.store.set_setting(names.SETTING, {"plans": {"sha256": "fixture"}})
        self.app.store.set_setting("characters", [{"id": "owner", "values": {"name": "Foerekthun", "race": "Orc"}}])
        self.request = {"provider_id": "fish-s21-free", "character_id": "owner", "submit": True,
                        "addons_folder": str(addons), "fetch_held_with_missing": True}
        self.row = {"source_id": "q:794:accept", "npc_id": 1, "npc": "Zureetha", "key": "794-accept",
                    "races": ["orc"], "area": "valley-of-trials", "stock_path": "794-accept.ogg",
                    "stock_text": "Go, Adventurer. Be swift.", "slot": [4, 14], "context": [0, 15]}
        self.activity = []
        self.paused = False
        self.checked = Mock()
        scope = self.enterContext(ExitStack())
        def mock(name, *args, **kwargs):
            return scope.enter_context(patch(name, *args, **kwargs))
        mock("spoken_player_names.imported", side_effect=lambda app, part, **kw: ({"dialogues":[self.row]}, {}) if part=="plans" else ({}, {}))
        self.playback = {"seals_by_dialogue": {}}
        mock("lite_store_playback.installed_playback", return_value=self.playback)
        mock("lite_store_playback.playable_saved_names", return_value={})
        mock("lite_store_playback.review_saved_returns", return_value=False)
        mock("lite_store_playback.name_present", return_value=True)
        mock("lite_paced_name.preflight_name_base", return_value=({}, {}, {}, {}))
        mock("spoken_player_names.load_name_areas", return_value={})
        mock("spoken_player_names.load_npc_factions", return_value={})
        mock("spoken_player_names.queue_areas", return_value=[self.row["area"]])
        mock("spoken_player_names.matches_character", return_value=True)
        mock("spoken_player_names.starting_greeting_area", return_value=None)
        mock("spoken_player_names.installed", return_value=self.root / "stock.ogg")
        mock("spoken_player_names.publish_snapshot")
        mock("lite_expanded_name_stage.checked_binding", self.checked)

    def fetch(self):
        return names.run(self.app, self.request, Mock(), should_stop=lambda:self.paused,
                         on_activity=self.activity.append)

    def fit_failure(self, identity="a"):
        return names.NameCutUnavailable("No measured cut fits.", request_id=identity * 64)

    def retries(self):
        return [(item["retake_attempt"],item["retake_total"]) for item in self.activity if "retake_attempt" in item]

    def test_area_and_selected_dialogues_make_three_retakes_then_continue(self):
        for selected in (False,True):
            with self.subTest(selected=selected):
                if selected:
                    self.request["source_keys"] = [names.source_key(self.row)]
                self.checked.reset_mock()
                self.activity.clear()
                self.checked.side_effect = [self.fit_failure(ch) for ch in "abcd"]
                result = self.fetch()
                self.assertEqual(self.checked.call_count,4)
                self.assertEqual(self.retries(),[(1,3),(2,3),(3,3)])
                self.assertEqual(result["statuses"][0]["state"],"held")
                self.assertEqual(result["statuses"][0]["retake_attempts"],3)

    def test_third_retake_can_succeed(self):
        self.checked.side_effect = [self.fit_failure(ch) for ch in "abc"] + [None]
        self.assertEqual(self.fetch()["statuses"][0]["state"],"staged")
        self.assertEqual(self.checked.call_count,4)
        self.assertEqual(self.retries(),[(1,3),(2,3),(3,3)])

    def test_four_unusable_wavs_leave_one_dialogue_held_and_the_next_ready(self):
        second = dict(self.row, source_id="q:795:accept", key="795-accept", stock_path="795-accept.ogg")
        def plan(app, part, **kwargs):
            return ({"dialogues": [self.row, second]}, {}) if part == "plans" else ({}, {})
        self.checked.side_effect = [names.InvalidNameTake("Silent WAV.", request_id="b"*64)] * 4 + [None]
        with patch.object(names, "imported", side_effect=plan):
            result = self.fetch()
        self.assertEqual([row["state"] for row in result["statuses"]], ["held", "staged"])
        self.assertEqual(result["statuses"][0]["retake_attempts"], 3)
        self.assertEqual(self.retries(), [(1,3), (2,3), (3,3)])
        self.assertEqual(self.checked.call_count, 5)

    def test_pause_before_retake(self):
        def fail(*args):
            self.paused=True
            raise self.fit_failure()
        self.checked.side_effect=fail
        self.assertEqual(self.fetch()["statuses"][0]["state"],"queued")
        self.assertEqual(self.checked.call_count,1)
        self.assertEqual(self.retries(),[])

    def test_game_and_recovered_fetch_retry_a_proven_bad_cut(self):
        self.request.pop("fetch_held_with_missing")
        for recovering in (False, True):
            with self.subTest(recovering=recovering):
                self.request["_resume_interrupted_fetch"] = recovering
                self.activity.clear()
                self.checked.side_effect = [self.fit_failure()] * 3 + [None]
                self.assertEqual(self.fetch()["statuses"][0]["state"], "staged")
                self.assertEqual(self.retries(), [(1,3), (2,3), (3,3)])

    def test_dialogue_times_cover_attempts_and_distinguish_cached_checks(self):
        self.checked.return_value = None
        for error in (None, ValueError("Missing catalog.")):
            with self.subTest(error=error):
                self.checked.side_effect = error
                with patch.object(names, "perf_counter", side_effect=[1, 2, 5.125, 6]):
                    result = self.fetch()
                row, = result["statuses"]
                self.assertEqual((row["npc"], row["source_id"], row["stage"]),
                                 ("Zureetha", "q:794:accept", "Quest offer"))
                self.assertEqual(row["elapsed_seconds"], 3.125)
                self.assertEqual(row["already_ready"], error is None)
                self.assertEqual(result["elapsed_seconds"], 5)
        self.paused = True
        skipped, = self.fetch()["statuses"]
        self.assertEqual(skipped["state"], "queued")
        self.assertNotIn("elapsed_seconds", skipped)

    def test_unknown_response_or_local_failure_does_not_trigger_retake(self):
        for error in (names.ReturnNeedsReview("Transfer uncertain."),ValueError("Missing catalog.")):
            with self.subTest(error=type(error).__name__):
                self.checked.reset_mock()
                self.checked.side_effect=error
                self.fetch()
                self.assertEqual(self.checked.call_count,1)
                self.assertEqual(self.retries(),[])

    def test_confirmed_replacement_is_one_deliberate_take(self):
        revision = "b" * 64
        seal = {"clock_contract_sha256": revision}
        self.playback["seals_by_dialogue"][(self.row["source_id"], self.row["npc_id"])] = [(None, seal)]
        self.request.pop("fetch_held_with_missing")
        self.request.update(confirmed_replace=True, replace_recording={"slot": revision + ":1", "sha256": "c"*64})
        self.checked.side_effect = self.fit_failure()
        with (patch("lite_recording_review.current_take"),
              patch("lite_paced_name.prepared_base", return_value=(None, {"sequence": [{"kind": "name", "occurrence": 1}]},seal,None))):
            self.assertEqual(self.fetch()["statuses"][0]["state"], "held")
        self.assertEqual(self.checked.call_count, 1)
        self.assertEqual(self.retries(), [])


class RetakeReceipts(unittest.TestCase):
    def setUp(self):
        self.fixture = fetch_provider_cooldown.ProviderCooldown()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.replies.pop(0)
        buffer = BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams((1, 2, 44100, 0, "NONE", ""))
            wav.writeframes(b"\x40\0\xc0\xff" * 2000)
        self.wav = buffer.getvalue()
        self.fixture.replies[0] = self.wav

    def obtain(self, name, *, row=None, cached_only=False, **kwargs):
        f = self.fixture
        scratch = f.root / name
        scratch.mkdir()
        with (patch.object(names, "prepare_alignment", return_value={}),
              patch.object(names, "make_alignment", return_value={"phones": []})):
            return names.obtain_fresh(SimpleNamespace(store_managed=True),
                row or {"source_id": "q:1:accept", "npc_id": 1, "stock_text": "Greetings, Adventurer."},
                "Greetings, Foerekthun.", f.profile, f.stock, f.root, scratch,
                False, True, Mock(), Mock(), cached_only=cached_only, retry_held=not cached_only,
                retry_missing=not cached_only, **kwargs)

    def test_only_failed_request_replaced_and_each_take_archived(self):
        f=self.fixture
        self.obtain("initial")
        self.obtain("different-occurrence",retake_request="c"*64)
        self.assertEqual(len(f.sent),1)
        for attempt in range(1,4):
            f.replies.append(self.wav)
            self.obtain("retake-"+str(attempt),retake_request="b"*64)
        self.assertEqual(len(f.sent),4)
        self.assertEqual(len(list((f.history/"previous-attempts").glob("*/receipt.json"))),3)

    def test_uncertain_submission_on_next_context_is_held(self):
        f=self.fixture
        f.replies=[ConnectionResetError("Lost response")]
        with self.assertRaises(names.ReturnNeedsReview):
            self.obtain("initial")
        for attempt,flags in enumerate(({},{"retake_request":"c"*64, "retry_uncertain":True})):
            with self.assertRaises(names.ReturnNeedsReview):
                self.obtain("uncertain-"+str(attempt),**flags)
        self.assertEqual(len(f.sent),1)

    def test_completed_bad_responses_can_be_replaced_with_the_same_request(self):
        f = self.fixture
        silent = self.wav[:44] + bytes(len(self.wav) - 44)
        f.replies = [b"", b"invalid WAV", silent, self.wav]
        flags = {"name_span": (11, 21)}
        for attempt in range(3):
            with self.assertRaises(names.InvalidNameTake) as raised:
                self.obtain(str(attempt), **flags)
            self.assertEqual(raised.exception.request_id, "b" * 64)
            receipt = json.loads((f.history / "receipt.json").read_bytes())
            self.assertEqual(receipt["state"], "unusable_return")
            flags["retake_request"] = raised.exception.request_id
        self.assertIsNotNone(self.obtain("good", **flags))
        archived = list((f.history / "previous-attempts").glob("*/receipt.json"))
        self.assertEqual(len(archived), 3)
        self.assertEqual({json.loads(p.read_bytes())["state"] for p in archived}, {"unusable_return"})
        self.assertEqual(json.loads((f.history / "receipt.json").read_bytes())["state"], "returned")
        self.assertEqual(len(f.sent), 4)

    def test_old_recording_edge_cut_reused_offline_until_rechecked(self):
        f = self.fixture
        row = {"source_id":"q:1:accept","npc_id":1,"stock_text":"Greetings, Adventurer.",
               "leading_word":{"source_text":"Greetings"}}
        self.obtain("initial", row=row)
        receipt = json.loads((f.history / "receipt.json").read_bytes())
        reason = "No measured phonetic cut fits the prepared slot and preserves its neighboring speech."
        marker = {"wav_sha256":receipt["wav_sha256"],"reason":reason}
        target = f.history / "hold.json"
        target.write_text(json.dumps(marker))
        self.assertIsNotNone(self.obtain("recheck",row=row,cached_only=True,name_span=(11,21)))
        for index, hold in enumerate(({**marker,"cut_review_version":2},{**marker,"reason":"Wrong name."})):
            target.write_text(json.dumps(hold))
            with self.assertRaises(names.ReturnNeedsReview):
                self.obtain("still-held-"+str(index),row=row,cached_only=True,name_span=(11,21))
        target.write_text(json.dumps(marker))
        self.assertIsNotNone(self.obtain("no-leading-word",cached_only=True,name_span=(11,21)))
        self.assertEqual(len(f.sent),1)

    def test_fish_timestamps_with_a_different_name_are_an_unusable_take(self):
        event = {"audio_base64": b64encode(self.wav).decode(), "chunk_seq": 0, "chunk_audio_offset_sec": 0,
                 "alignment": {"segments": [{"text": "Greetings, Edwin.", "start": 0.0, "end": 0.04}]}}
        with self.assertRaisesRegex(names.UnusableProviderAudio, "requested name"):
            names.check_provider_audio(SimpleNamespace(engine="fish", timed=True),
                                       ("data: " + json.dumps(event) + "\n\n").encode(),
                                       "Greetings, Foerekthun.", (11, 21))

    def test_resuming_a_legacy_confirmed_silent_take(self):
        f = self.fixture
        f.history.mkdir()
        ident = "b" * 64
        (f.history / "receipt.json").write_text(json.dumps({
            "state": "return_requires_inspection", "request_sha256": ident, "provider_id": f.profile.ident,
            "source_id": "q:1:accept", "reason": "The provider returned silent audio."}))
        with self.assertRaises(names.InvalidNameTake):
            self.obtain("resume", resume_interrupted_fetch=True)
        self.assertFalse(f.sent)
        self.assertIsNotNone(self.obtain("retake", retake_request=ident, resume_interrupted_fetch=True))
        self.assertEqual(len(f.sent), 1)

    def test_provider_detects_both_short_body_and_truncated_chunk(self):
        from unittest.mock import MagicMock
        from lite_name_providers import NameProvider
        profile = object.__new__(NameProvider)
        profile.shared, profile.transport, profile.lossless_references = None, None, False
        profile.engine = "fish"
        wire = {"url": "https://example.invalid/audio", "headers": {}, "body": b"test"}
        for body in (b"RIFF", IncompleteRead(b"RIFF", 96)):
            response = MagicMock()
            response.headers = {"Content-Length": "100"}
            response.__enter__.return_value = response
            response.read1.side_effect = [body, b""]
            opener = Mock()
            opener.open.return_value = response
            with self.subTest(body=type(body).__name__), self.assertRaises(names.UnusableProviderAudio):
                profile.obtain(wire, opener)


if __name__ == "__main__":
    unittest.main()
