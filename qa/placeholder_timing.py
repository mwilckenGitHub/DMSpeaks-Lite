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
                 grammar_phones="AH D V EH N CH ER ER", keyword_phones="AH D V EH N CH ER ER",
                 following_name=None):
        searches = []

        def decode(config, dictionary, search, wave):
            searches.append(search[0])
            timings, phones = (grammar, grammar_phones) if search[0] == "-jsgf" else (keyword, keyword_phones)
            entries = [line.split(" ", 1) for line in dictionary.read_text(encoding="ascii").splitlines()]
            begin, duration, score = timings
            if search[0] == "-jsgf" and following_name is not None:
                placeholder = next((key for key, value in entries if key.startswith("a") and value == phones), None)
                if placeholder is None:
                    return []
                target = next(key for key, value in entries if key.startswith("t") and value == "T EH S T N EY M")
                return [{"t": placeholder, "b": begin, "d": duration, "p": score},
                        {"t": target, "b": following_name, "d": .88, "p": .26}]
            label = next(key for key, value in entries if value == phones)
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
        self.assertEqual(searches, ["-jsgf", "-jsgf"])

    def test_extra_full_placeholder_before_the_correct_name(self):
        result, searches = self.evidence(onset=1.33, grammar=(.60, .63, 1.0),
                                         keyword=(.60, .63, .91), following_name=1.48)
        self.assertEqual(searches, ["-jsgf", "-jsgf", "-kws"])
        self.assertEqual(result["keyword_start_seconds"], .45)

    def test_extra_placeholder_needs_corroboration_before_the_measured_name(self):
        for keyword in [(.60, .63, .81), (.67, .63, .91), (.60, .91, .91)]:
            with self.subTest(keyword=keyword):
                self.assertIsNone(self.evidence(onset=1.33, grammar=(.60, .63, 1.0),
                                                keyword=keyword, following_name=1.48)[0])
        result, searches = self.evidence(onset=1.8, grammar=(.60, .63, 1.0),
                                         keyword=(.60, .63, .91), following_name=1.48)
        self.assertIsNone(result)
        self.assertEqual(searches, ["-jsgf", "-jsgf"])


if __name__ == "__main__":
    unittest.main()
