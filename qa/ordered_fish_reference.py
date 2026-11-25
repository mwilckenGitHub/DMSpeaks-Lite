"""Check the actual wire audio and transcript with tiny local PCM inputs."""
from hashlib import sha256
from io import BytesIO
import unittest
import wave

from fish_references import joined_fish_reference


def source(identity, text, frames):
    file = BytesIO()
    with wave.open(file, "wb") as recording:
        recording.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        recording.writeframes(frames)
    return {"id": identity, "transcript": text, "wav_bytes": file.getvalue()}


class OrderedFishReference(unittest.TestCase):
    def test_joins_voice_and_original_in_the_same_order_as_their_text(self):
        voice_pcm, original_pcm = bytes([1, 0]) * 7, bytes([2, 0]) * 13
        voice = source("voice", "Speaker sample.", voice_pcm)
        original = source("dialogue", "Complete original.", original_pcm)
        for inputs, expected_pcm in (([voice, original], voice_pcm + original_pcm), ([voice], voice_pcm)):
            with self.subTest(input_ids=[item["id"] for item in inputs]):
                result, evidence, _ = joined_fish_reference(inputs)
                with wave.open(BytesIO(result["wav_bytes"])) as output:
                    self.assertEqual(output.readframes(output.getnframes()), expected_pcm)
                self.assertEqual(result["transcript"], "\n\n".join(item["transcript"] for item in inputs))
                self.assertEqual([part["id"] for part in evidence], [item["id"] for item in inputs])
                self.assertEqual([part["audio_sha256"] for part in evidence],
                                 [sha256(item["wav_bytes"]).hexdigest() for item in inputs])
