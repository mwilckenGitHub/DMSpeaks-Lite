"""Replay a definite provider throttle and exercise pause and resume without network I/O."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "source"))
import spoken_player_names as names


class ProviderCooldown(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stock = self.root / "stock.ogg"
        self.stock.write_bytes(b"stock reference")
        self.history = self.root / ("b" * 64)
        self.clock = 100.0
        self.elapsed = 0.0
        self.sent_elapsed = []
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setparams((1, 2, 44100, 0, "NONE", ""))
            wav.writeframes(b"\1\0" * 800)
        self.sent = []
        self.paused = False
        error = HTTPError("https://example.invalid", 429, "rate limit", {"Retry-After": "4"}, io.BytesIO())
        self.replies = [error, audio.getvalue()]
        self.profile = SimpleNamespace(engine="azure", timed=False, ident="fixture",
            request=lambda *args: {"body": b"fixture"}, identity=lambda *args: "b" * 64,
            normalized_audio=lambda raw: raw, obtain=self.obtain)
        self.messages = []

    def obtain(self, wire, opener, on_send):
        self.sent.append(self.clock)
        self.sent_elapsed.append(self.elapsed)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def fetch(self, *, cancel_wait=False, cached_only=False, clock_adjustment=0):
        def sleep(seconds):
            nonlocal clock_adjustment
            self.clock += seconds
            self.elapsed += seconds
            self.clock += clock_adjustment
            clock_adjustment = 0
            if cancel_wait:
                self.paused = True
        with (patch.object(names, "prepare_alignment", return_value={}),
              patch.object(names, "make_alignment", return_value={"phones": []}),
              patch.object(names, "time", side_effect=lambda: self.clock),
              patch.object(names, "monotonic", create=True, side_effect=lambda: self.elapsed),
              patch.object(names, "sleep", side_effect=sleep)):
            return names.obtain_fresh(SimpleNamespace(store_managed=True),
                {"source_id": "q:7:accept", "npc_id": 197, "stock_text": "Welcome, Adventurer."},
                "Welcome, Rowan.", self.profile, self.stock, self.root, self.root,
                False, True, Mock(), Mock(), cached_only=cached_only, should_stop=lambda: self.paused,
                on_rate_limit_wait=self.messages.append)

    def test_wait_and_return_one_verified_response(self):
        self.assertIsNotNone(self.fetch())
        self.assertEqual(self.sent, [100, 104])
        self.assertEqual(self.messages, [104])
        self.assertEqual(json.loads((self.history / "receipt.json").read_bytes())["state"], "returned")
        self.assertEqual(len(list(self.history.glob("rejected-*.json"))), 1)

    def test_pause_during_throttle_and_resume_from_receipt(self):
        self.assertIsNone(self.fetch(cancel_wait=True))
        self.assertEqual(self.sent, [100])
        self.assertEqual(json.loads((self.history / "receipt.json").read_bytes())["retry_at"], 104)
        self.paused = False
        self.assertIsNone(self.fetch(cached_only=True))
        self.assertIsNotNone(self.fetch())
        self.assertEqual(self.sent, [100, 104])

    def test_clock_steps_during_a_provider_wait(self):
        for adjustment in (-90, 90):
            with self.subTest(clock_adjustment=adjustment):
                fixture = ProviderCooldown()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                self.assertIsNotNone(fixture.fetch(clock_adjustment=adjustment))
                self.assertEqual(fixture.sent_elapsed, [0, 4])

    def test_clock_set_back_while_paused_respects_the_original_wait(self):
        self.assertIsNone(self.fetch(cancel_wait=True))
        self.clock -= 3600
        self.paused = False
        before = self.elapsed
        self.assertIsNotNone(self.fetch())
        self.assertEqual(self.elapsed - before, 4)

    def test_headerless_throttling_backs_off_and_caps_delay(self):
        self.replies = [
            HTTPError("https://example.invalid", 429, "rate limit", {}, io.BytesIO())
            for _ in range(7)
        ] + [self.replies[-1]]
        self.assertIsNotNone(self.fetch())
        self.assertEqual(self.sent, [100, 130, 190, 310, 550, 850, 1150, 1450])
        self.assertEqual(self.messages, self.sent[1:])

    def test_auth_and_payment_errors_are_not_retried(self):
        for code in (401, 402, 403):
            with self.subTest(code=code):
                self.replies = [HTTPError("https://example.invalid", code, "rejected", {}, io.BytesIO())]
                with self.assertRaises(names.ProviderRejected):
                    self.fetch()
                self.assertEqual(self.messages, [])
                self.assertEqual(json.loads((self.history / "receipt.json").read_bytes())["http_status"], code)
        self.assertEqual(self.sent, [100, 100, 100])

    def test_ambiguous_transfer_stays_held(self):
        self.replies = [ConnectionResetError("lost transfer")]
        with self.assertRaises(names.ReturnNeedsReview):
            self.fetch()
        self.assertEqual(self.sent, [100])
        self.assertEqual(json.loads((self.history / "receipt.json").read_bytes())["state"], "return_requires_inspection")
        self.assertEqual(self.messages, [])

if __name__ == "__main__":
    unittest.main()
