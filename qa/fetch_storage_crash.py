"""Stop a real child process during file commits, then reopen the disposable profile."""
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
import audio_return_cache as returns
import lite_store_playback as playback
import spoken_player_names as names
import numpy as np
import soundfile as sf

IDENT = "b" * 64
CLOCK = "a" * 64
EXIT = 53


def fetch(root, *, recovering):
    scratch = root / ("resume" if recovering else "initial")
    scratch.mkdir()
    stock = root / "stock.ogg"
    stock.write_bytes(b"stock fixture")
    def obtain(wire, opener, on_send):
        with (root / "post-count").open("ab") as log:
            log.write(b"x")
        if recovering:
            raise AssertionError("Recovery sent a second speech request")
        raw = BytesIO()
        with wave.open(raw, "wb") as wav:
            wav.setparams((1, 2, 44100, 0, "NONE", ""))
            wav.writeframes(b"\x40\0\xc0\xff" * 2000)
        return raw.getvalue()
    profile = SimpleNamespace(engine="azure", timed=False, ident="fixture",
        request=lambda *args: {"body": b"fixture"}, identity=lambda *args: IDENT,
        normalized_audio=lambda raw: raw, obtain=obtain)
    with (patch.object(names, "prepare_alignment", return_value={}),
          patch.object(names, "make_alignment", return_value={"phones": []})):
        return names.obtain_fresh(SimpleNamespace(store_managed=True),
            {"source_id":"q:1:accept", "npc_id":1, "stock_text":"Greetings, Adventurer."},
            "Greetings, Rowan.", profile, stock, root, scratch, False, True, Mock(), Mock(),
            cached_only=False, retry_held=True, retry_missing=True, resume_interrupted_fetch=recovering)


def publish(root, name, *, queue=None):
    pack = root / playback.PLAYBACK
    pack.parent.mkdir(exist_ok=True)
    pack.write_bytes(b"fixture catalog")
    media = BytesIO()
    sf.write(media, np.sin(np.arange(1000) * .2), 44100, format="OGG", subtype="VORBIS")
    raw = media.getvalue()
    original = {"kind":"name", "occurrence":1}
    current = {"spoken_key":"quest", "sequence":[original], "source_id":"q:1:accept"}
    clip = {"file":"name.ogg", "sha256":sha256(raw).hexdigest(), "samples":1000}
    if queue:
        receipt = json.loads((queue / IDENT / returns.RECEIPT).read_bytes())
        playback.pending_return_cleanup(root, queue, {"clock_contract_sha256":CLOCK}, current["sequence"], name,
            [{"request_sha256": IDENT, "occurrence": 1, "raw_sha256": receipt["wav_sha256"], "clip": clip}])
    with patch("lite_name_bridge.ensure_status_addon"):
        playback.publish_names(root, {"store_index_sha256":sha256(pack.read_bytes()).hexdigest()},
            {"clock_contract_sha256":CLOCK}, current, {"sequence":[{**original, "choices":{name:clip}}]},
            {"bindings":[{"sequence_index_1based":1, "choices":{name:name}}]}, name, {"name.ogg":raw}, request="c"*64)


def crash_child(root, checkpoint):
    if checkpoint in {"pending-receipt", "verified-wav"}:
        write = returns.replace_private
        def exit_after_write(path, content):
            write(path, content)
            if path.name == (returns.PENDING if checkpoint == "pending-receipt" else returns.WAV):
                os._exit(EXIT)
        with patch.object(returns, "replace_private", side_effect=exit_after_write):
            fetch(root, recovering=False)
    else:
        write = playback.write_placement
        def exit_after_write(path, content):
            write(path, content)
            if (path.name == checkpoint or checkpoint == "audio" and path.suffix == ".ogg"
                    or checkpoint == "after-placement" and path.name == "Names.lua"):
                os._exit(EXIT)
        with patch.object(playback, "write_placement", side_effect=exit_after_write):
            publish(root, "rowan", queue=root / "requests" if checkpoint == "after-placement" else None)
    raise AssertionError("The injected fault was not reached")


class StorageCrashTests(unittest.TestCase):
    def child(self, root, checkpoint):
        process = subprocess.run([sys.executable, "-I", "-B", __file__, "crash", str(root), checkpoint],
                                  capture_output=True, text=True, timeout=40)
        self.assertEqual(process.returncode, EXIT, process.stderr)

    def test_verified_return_reused_without_second_post_and_cleaned_after_confirmation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.child(root, "verified-wav")
            history = root / IDENT
            old = json.loads((history / returns.RECEIPT).read_bytes())
            self.assertEqual(old["state"], "submitting")
            original = (history / returns.WAV).read_bytes()
            recovered, _, ident, _, _ = fetch(root, recovering=True)
            self.assertEqual(ident, IDENT)
            self.assertEqual(recovered.read_bytes(), original)
            self.assertEqual((root / "post-count").read_bytes(), b"x")
            final = json.loads((history / returns.RECEIPT).read_bytes())
            self.assertEqual(final["state"], "returned")
            self.assertFalse((history / returns.PENDING).exists())
            returns.finish_return(history, final)
            returns.finish_return(history, final)
            self.assertFalse((history / returns.WAV).exists())
            self.assertTrue((history / returns.RECEIPT).is_file())

    def test_missing_or_mismatched_wav_remains_uncertain(self):
        for stale in (False, True):
            with self.subTest(stale=stale), TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.child(root, "pending-receipt")
                history = root / IDENT
                if stale:
                    (history / returns.WAV).write_bytes(b"unrelated earlier audio")
                with self.assertRaises(names.ReturnNeedsReview):
                    fetch(root, recovering=True)
                self.assertEqual((root / "post-count").read_bytes(), b"x")
                self.assertFalse((history / returns.PENDING).exists())
                self.assertEqual(json.loads((history / returns.RECEIPT).read_bytes())["state"], "submitting")

    def test_overlay_restarts_before_and_after_each_publication_boundary(self):
        slot = CLOCK + ":1"
        for checkpoint in ("audio", playback.JOURNAL.name, "Names.json", "Names.lua"):
            with self.subTest(checkpoint=checkpoint), TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                publish(root, "existing")
                self.child(root, checkpoint)
                playback.restore_loader(root)
                playback.restore_loader(root)
                index = playback.playable_saved_names(root)
                self.assertIn("existing", index[slot])
                self.assertEqual("rowan" in index[slot], checkpoint in ("Names.json", "Names.lua"))
                self.assertFalse((root / playback.JOURNAL).exists())
                for name, clip in index[slot].items():
                    raw = (root / playback.name_audio_relative(clip)).read_bytes()
                    self.assertEqual(sha256(raw).hexdigest(), clip["sha256"])
                    with sf.SoundFile(BytesIO(raw)) as audio:
                        self.assertEqual((audio.frames, audio.samplerate), (1000, 44100))

    def test_disk_full_before_audio_or_loader_is_retryable_without_losing_prior_choices(self):
        for fault in ("audio", "Names.json", "Names.lua"):
            with self.subTest(fault=fault), TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                publish(root, "existing")
                write = playback.write_placement
                def disk_full(path, data):
                    if path.name == fault or fault == "audio" and path.suffix == ".ogg":
                        raise OSError(28, "No space left on device")
                    return write(path, data)
                with patch.object(playback, "write_placement", side_effect=disk_full):
                    with self.assertRaises(OSError):
                        publish(root, "rowan")
                playback.restore_loader(root)
                self.assertIn("existing", playback.playable_saved_names(root)[CLOCK+":1"])
                publish(root, "rowan")
                self.assertEqual(set(playback.playable_saved_names(root)[CLOCK+":1"]), {"existing", "rowan"})

    def test_restarted_cleanup_only_discards_an_exact_delivered_take(self):
        for changed in ("none", "retained", "new-take", "different-file", "replaced-ogg"):
            with self.subTest(changed=changed), TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                queue = root / "requests"
                history = queue / IDENT
                history.mkdir(parents=True)
                wav = b"recorded fixture"
                receipt = {"request_sha256":IDENT, "retained":changed == "retained"}
                returns.record_return(history, wav, receipt)
                publish(root, "existing")
                self.child(root, "after-placement")
                if changed == "new-take":
                    returns.record_return(history, b"a different take", receipt)
                elif changed == "different-file":
                    (history / returns.WAV).write_bytes(b"a different take")
                elif changed == "replaced-ogg":
                    entry = playback.saved_names(root)[CLOCK + ":1"]["rowan"]
                    (root / playback.name_audio_relative(entry)).write_bytes(b"damaged Ogg")
                playback.finish_placed_returns(root, queue)
                playback.finish_placed_returns(root, queue)
                self.assertEqual((history / returns.WAV).is_file(), changed != "none")
                self.assertEqual((history / returns.RECEIPT).is_file(), True)
                if changed == "none":
                    self.assertEqual(list(playback.cleanup_folder(root, queue).glob("*.json")), [])


if __name__ == "__main__":
    if sys.argv[1:2] == ["crash"]:
        crash_child(Path(sys.argv[2]), sys.argv[3])
    else:
        unittest.main()
