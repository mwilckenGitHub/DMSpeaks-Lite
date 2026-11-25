"""Exercise both splice strategies through Vorbis encoding and the installed name manifest."""
from pathlib import Path
from shutil import which
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
from guide_bindings import RATE
from lite_card_transfer import CardTransfer, card_fingerprints
from lite_clip_bounds import boundary_method, clip_bounds, saved_cuts
from lite_portable_io import file_hash
from lite_portable_join import phone_candidates, splice, trim_donor
from lite_portable_render import write_clip
import lite_store_playback as placement
from lite_store_playback import PLAYBACK, STATUS, name_audio_relative, playable_saved_names, publish_names
from vorbis_pcm import measure_vorbis, single_stream_end


def word(text, phone, start, stop):
    return dict(word=text, start_sample=start, end_sample=stop,
                phonemes=[dict(phone=phone, start_sample=start, end_sample=stop)])


class SavedClipBounds(unittest.TestCase):
    def check_placement(self, decay):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            work, addons = root / "render", root / "game/Interface/AddOns"
            work.mkdir()
            playback = addons / PLAYBACK
            playback.parent.mkdir(parents=True)
            playback.write_bytes(b"{}")
            # Keep an audible leading word on each recording. Their durations differ.
            source = (.16 * np.sin(np.arange(32000) * .041)).astype("float32")
            donor = (.14 * np.sin(np.arange(41000) * .057)).astype("float32")
            src_words = [word("age", "JH", 0, 9000), word("Adventurer", "ER", 9000, 29500)]
            words = [word("age", "JH", 0, 7000), word("Veylora", "AH", 7000, 35200)]
            if decay:
                donor[34600:35200] *= np.linspace(1, 0, 600)
                donor[35200:] = 0
            wav = work / "returned.wav"
            sf.write(wav, donor, RATE, subtype="FLOAT")
            donor, rate = sf.read(wav, dtype="float32")
            self.assertEqual(rate, RATE)
            starts, ends = phone_candidates(donor, words, 0, 2, 1, 2)
            trim_donor(donor, words, starts, ends)
            options = splice(source, src_words, dict(phones=words, samples=donor, starts=starts, ends=ends,
                              begin=0, extra=0), 0, 2, 1, 2, [0, len(source)], existing_leader=1)
            _, _, (start, stop), edges = options[0]
            method = boundary_method(edges["tail"], stop, trailing=True)
            self.assertEqual(method, "waveform_decay" if decay else "independent_name_boundary")
            self.assertEqual(start, 0)
            self.assertGreaterEqual(stop, words[-1]["end_sample"])
            self.assertLess(stop, len(donor))
            clip = write_clip(work, "personal-name", donor[start:stop])
            cut = clip_bounds(file_hash(wav), file_hash(wav), len(donor), start, stop,
                              boundary_method(edges["lead"], start, trailing=False), method)
            clip["cut"] = cut
            manifest = dict(source_id="q:4641:accept", spoken_key="4641-accept",
                            sequence=[dict(kind="name", occurrence=1)])
            clock = {"sequence": [{"choices": {"veylora": clip}}]}
            caption = {"bindings": [{"sequence_index_1based": 1, "choices": {"veylora": "age, Veylora"}}]}
            seal = {"clock_contract_sha256": "a" * 64}
            payloads = {clip["file"]: (work / clip["file"]).read_bytes()}
            state = {"store_index_sha256": file_hash(playback)}
            publish_names(addons, state, seal, manifest, clock, caption, "veylora", payloads, request="fetch")
            saved = playable_saved_names(addons)["a"*64 + ":1"]["veylora"]
            self.assertEqual(saved["cut"], cut)
            self.assertEqual((cut["start_seconds"], cut["stop_seconds"]), (start / RATE, stop / RATE))
            output = addons / name_audio_relative(saved)
            self.assertEqual(file_hash(output), clip["sha256"])
            self.assertEqual(measure_vorbis(output), (stop - start, RATE, 1))
            self.assertEqual(single_stream_end(output), stop - start)
            decoded, _ = sf.read(output)
            self.assertGreater(np.corrcoef(decoded, donor[start:stop])[0, 1], .99)
            if decoder := which("ffmpeg"):
                pcm = subprocess.check_output([decoder, "-nostdin", "-v", "error", "-i", str(output),
                                               "-f", "f32le", "pipe:1"])
                self.assertEqual(len(pcm), (stop - start) * 4)
            lua = (addons / STATUS / "Names.lua").read_text()
            self.assertIn(f"startSample = {start}, stopSample = {stop}, sampleRate = {RATE}", lua)

            # An interrupted index update resumes from committed JSON, including timings.
            legacy = {key: value for key, value in saved.items() if key != "cut"}
            slot = "a" * 64 + ":1"
            old = {slot: {"veylora": legacy}}
            rich = {slot: {"veylora": saved}}
            write = placement.write_placement
            def interrupt_lua(path, data):
                if path.name == "Names.lua":
                    raise OSError("simulated interrupted write")
                write(path, data)
            with patch.object(placement, "write_placement", side_effect=interrupt_lua), self.assertRaises(OSError):
                placement.write_name_index(addons, old)
            placement.restore_loader(addons)
            self.assertEqual((addons / STATUS / "Names.lua").read_bytes(), placement.lua_index(old))

            # Import the exact same Ogg from a richer backup without copying the media.
            transfer = object.__new__(CardTransfer)
            transfer.addons = addons
            self.assertNotEqual(card_fingerprints([], old), card_fingerprints([], rich))
            self.assertEqual(transfer.merge_audio(rich, work), (0, 1, 0))
            self.assertEqual(transfer.merge_audio(old, work), (0, 1, 0))
            self.assertEqual((addons / STATUS / "Names.lua").read_bytes(), placement.lua_index(rich))
            self.assertEqual(playable_saved_names(addons), rich)
            self.assertEqual(file_hash(output), saved["sha256"])

    def test_phonetic_join_to_saved_ogg(self):
        self.check_placement(False)

    def test_wave_decay_to_saved_ogg(self):
        self.check_placement(True)

    def test_legacy_and_inconsistent_bounds(self):
        self.assertEqual(saved_cuts({"samples": 8}), {})
        cut = clip_bounds("0" * 64, "1" * 64, 4000, 2, 10, "phoneme", "waveform_decay")
        for changed in ({"stop_sample": 11}, {"stop_seconds": 0}, {"donor_frames": 9}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                saved_cuts({"cut": {**cut, **changed}, "samples": 8})


if __name__ == "__main__":
    unittest.main()
