"""Run with Python 3 from a source checkout. No model, audio or account is required."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

import name_placeholder_audio as gate


class PlaceholderTimingTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dictionary = Path(temporary.name) / "test.dict"
        self.dictionary.write_text("welcome W EH L K AH M\ntestname T EH S T N EY M\n", encoding="ascii")

    def evidence(self, *, onset=.92, grammar=(.79, .49, .79), keyword=(.78, .50, .86),
                 grammar_phones="AH D V EH N CH ER ER", keyword_phones="AH D V EH N CH ER ER"):
        searches = []

        def decode(config, dictionary, search, wave):
            searches.append(search[0])
            timings, phones = (grammar, grammar_phones) if search[0] == "-jsgf" else (keyword, keyword_phones)
            entries = [line.split(" ", 1) for line in dictionary.read_text(encoding="ascii").splitlines()]
            label = next(key for key, value in entries if value == phones)
            begin, duration, score = timings
            return [{"t": label, "b": begin, "d": duration, "p": score}]

        with patch.object(gate, "_tool_run"), patch.object(gate, "_recognize", side_effect=decode):
            result = gate.placeholder_evidence(
                Path("example.ogg"), ["welcome", "testname"], round(onset * gate.RATE),
                {"dictionary": self.dictionary},
            )
        return result, searches

    def test_forced_onset_inside_corroborated_whole_stock_word(self):
        result, searches = self.evidence()
        self.assertEqual(searches, ["-jsgf", "-kws"])
        self.assertEqual(result["supporting_phones"], "AH D V EH N CH ER ER")
        self.assertEqual(result["keyword_start_seconds"], .630)

    def test_ordinary_word_onset(self):
        self.assertIsNotNone(self.evidence(onset=.64)[0])

    def test_late_onset_requires_two_complete_words_at_matching_boundaries(self):
        for parameters in [
            {"keyword": (.69, .59, .90)},     # Different starts
            {"keyword": (.78, .38, .90)},     # Different ends
            {"keyword": (.78, .50, .80)},     # Weak independent corroboration
            {"keyword_phones": "AH D V EH N"},
            {"grammar": (.79, .49, .65)},
            {"onset": 1.38},                  # Stock word already ended
        ]:
            with self.subTest(parameters=parameters):
                self.assertIsNone(self.evidence(**parameters)[0])

    def test_spoken_target_ends_check_before_keyword_search(self):
        result, searches = self.evidence(grammar_phones="T EH S T N EY M")
        self.assertIsNone(result)
        self.assertEqual(searches, ["-jsgf"])


if __name__ == "__main__":
    unittest.main()
