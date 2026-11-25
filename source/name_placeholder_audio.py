"""Check a delivered English name clip for audible stock 'adventurer' speech."""
from functools import lru_cache
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from phoneme_process import inference_command as _inference_command, run_tool as _tool_run

RATE = 44100


_VOWELS = frozenset("AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW".split())
_ONSET = ("AE D V EH", "AH D V EH", "AE D V EH N", "AH D V EH N")
_STOCK = ("AE D V EH N CH ER ER", "AH D V EH N CH ER ER", "AE D V EH N T", "AH D V EH N T")
# Broad non-placeholder competitors keep a weak match to an unusual name from
# being forced onto the specific stock consonants.
_OTHER = ("AH V EH", "AH N", "AH", "AH D", "AH B EH", "AH T EH", "AH D IH", "AH D EH",
          "AE V EH", "EH D", "IH", "V EH", "EH", "IH N", "S EH", "S IH", "SH EH", "SH IH",
          "F EH", "TH EH")
_STOCK_INTERIOR = "V EH N CH ER"
_KEYWORDS = ("AE D V EH N", "AH D V EH N", "AE D V EH", "AH D V EH", "V EH N", *_STOCK[:2], _STOCK_INTERIOR)


@lru_cache(maxsize=2)
def _dictionary(path, modified, size):
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) > 1 and "(" not in fields[0]:
            entries[fields[0].casefold()] = tuple(fields[1:])
    return entries


def _pronunciations(words, path, supplied):
    info = path.stat()
    dictionary = _dictionary(path, info.st_mtime_ns, info.st_size)
    result = []
    for word in words:
        key = word.casefold().replace("’", "'")
        phones = supplied.get(key)
        if phones is None:
            phones = dictionary.get(key)
        if phones is None:
            from english_name_pronunciation import Pronouncer
            phones = Pronouncer().pronunciation(key)
        if isinstance(phones, str):
            phones = phones.split()
        if not phones or any(not re.fullmatch("[A-Z]{1,2}", p) for p in phones):
            return None
        result.append(tuple(phones))
    return result


def _alternatives(phones):
    first_vowel = next((i for i, phone in enumerate(phones) if phone in _VOWELS), None)
    if first_vowel is None:
        return {}
    first = " ".join(phones[:7])
    # An intended 'Adven...', 'Vent...' or comparable onset can be the name itself.
    if first.startswith(("AE D", "AH D", "EH D", "AE V", "AH V", "V", "F EH N")) or "D V" in first:
        return {}
    target = {phones}
    for vowel in ("AE", "AH", "AA", "EH", "IH"):
        target.add((*phones[:first_vowel], vowel, *phones[first_vowel + 1:]))
    leaks = {tuple(item.split()) for item in _STOCK}
    other = set()
    for index in range(1, len(phones)):
        if phones[index - 1] not in _VOWELS or phones[index] in _VOWELS:
            continue
        tail = phones[index:]
        if sum(p in _VOWELS for p in phones[:index]) > 2 or sum(p in _VOWELS for p in tail) < 2:
            continue
        leaks.update((*prefix.split(), *tail) for prefix in _ONSET)
        other.update((*prefix.split(), *tail) for prefix in _OTHER)
    return {kind + str(i): " ".join(value)
            for kind, values in (("t", target), ("l", leaks - target), ("x", other - target - leaks))
            for i, value in enumerate(sorted(values))}


def _recognize(config, dictionary, search, wave):
    args = ["-hmm", str(config["acoustic_model"]), "-dict", str(dictionary), *search,
            "-loglevel", "ERROR", "single", str(wave)]
    command = _inference_command(config["executable"], args, config.get("inference_lease"))
    result = _tool_run(command, 30)
    return [word for line in result.stdout.splitlines() if line.strip()
            for word in json.loads(line).get("w", [])]


def _intervening_vowel(ogg, onset):
    """Find a voiced nucleus between two frication intervals at a measured name onset."""
    import numpy as np
    import soundfile as sf

    step, width = round(RATE * .010), round(RATE * .025)
    start = max(0, onset - round(RATE * .270))
    with sf.SoundFile(ogg) as clip:
        if clip.samplerate != RATE or clip.channels != 1 or onset >= len(clip):
            return None
        clip.seek(start)
        audio = clip.read(onset - start + round(RATE * .150) + width, dtype="float32")
    window = np.hanning(width)
    frequencies = np.fft.rfftfreq(width, 1 / RATE)
    low_band = (frequencies >= 90) & (frequencies < 1100)
    high_band = (frequencies >= 3000) & (frequencies < 8500)
    friction, voiced = [], []
    for offset in range(0, len(audio) - width, step):
        frame = audio[offset:offset + width] * window
        power = np.mean(frame ** 2)
        spectrum = np.abs(np.fft.rfft(frame)) ** 2
        low, high = spectrum[low_band].sum(), spectrum[high_band].sum()
        friction.append(high > low * 3 and power > 1e-6)
        periodic = False
        if low > high * 6 and power > 1e-5:
            corr = np.correlate(frame, frame, "full")[width - 1:]
            periodic = corr[round(RATE/320):round(RATE/60)].max() > .29 * corr[0]
        voiced.append(periodic)
    # A real extra vowel has its own nucleus after the completed consonant
    # and before the target sibilant. Quiet/effect tails cannot act as nuclei.
    sustained = [i for i in range(len(friction)-2) if all(friction[i:i+3])]
    for i in range(3, len(voiced)-3):
        middle = start + i * step
        if middle > onset or not all(voiced[i:i+4]):
            continue
        if not any(i-8 <= left <= i-3 for left in sustained):
            continue
        for right in sustained:
            following = start + right * step
            if i+4 <= right <= i+12 and abs(following-onset) <= .065*RATE:
                return middle / RATE, following / RATE
    return None


def _extra_syllable(ogg, onset, phones, config, wave, dictionary, grammar, leaders):
    # Only these adjacent consonants supply independent waveform landmarks.
    # The preceding affricate/fricative must have finished and the name must
    # audibly begin with its own sibilant after the intervening vowel.
    if len(phones) < 2 or phones[-2][-1] not in {"CH","JH","S","SH"} or phones[-1][0] not in {"S","SH"}:
        return None
    landmarks = _intervening_vowel(ogg, onset)
    if not landmarks:
        return None
    target = phones[-1]
    vowel = next(i for i,p in enumerate(target) if p in _VOWELS)
    target_variants = [target, *((*target[:vowel], phone, *target[vowel+1:])
                                for phone in ("AE","AH","AA","EH","IH"))]
    entries = {label: [value] for label,value in leaders.items()}
    entries.update(nm=list(dict.fromkeys(" ".join(v) for v in target_variants)),
                   ix=["AH D","AE D","AH T","AE T","EH D"],
                   noise=["AH","AE","AH N","EH N","IH Z","AH V","AH B","AH K","AH P","EH"])
    lines = []
    for key, alternatives in entries.items():
        for i,p in enumerate(alternatives):
            label = key if i==0 else f"{key}({i+1})"
            lines.append(f"{label} {p}\n")
    dictionary.write_text("".join(lines), encoding="ascii")
    line = " ".join([*leaders, "[(ix | noise)]", "nm"])
    grammar.write_text(f"#JSGF V1.0;\ngrammar name;\npublic <line> = {line};\n", encoding="ascii")
    decoded = _recognize(config, dictionary, ["-jsgf",str(grammar)], wave)
    spoken = [w for w in decoded if not w["t"].startswith("<")]
    if len(spoken) != len(leaders)+2 or not spoken[-1]["t"].startswith("nm"):
        return None
    extra = spoken[-2]
    if not extra["t"].startswith("ix"):
        return None
    vowel_at, name_at = landmarks
    begin = extra["b"] - .150
    end = begin + extra["d"]
    if not begin-.025 <= vowel_at <= end or abs(end-name_at) > .055:
        return None
    return dict(hypothesis="extra syllable before name", word_score=extra["p"],
                vowel_start_seconds=round(vowel_at,3), next_frication_seconds=round(name_at,3),
                name_start_sample=onset)

def placeholder_evidence(ogg, words, name_start_sample, config, *, pronunciations=None):
    """Return corroborated acoustic evidence, or None for a clean/ambiguous clip.

    Words must cover the owned phrase from its first word through one final
    single-word name. The onset uses the same 44.1 kHz clock as the delivered Ogg.
    A keyword or separate voiced nucleus must support the decoder at that onset.
    """
    if (not words or len(words) > 16 or type(name_start_sample) is not int or name_start_sample < 0
            or not all(re.fullmatch(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", w) for w in words)
            or config.get("phone_set", "ARPAbet").casefold() != "arpabet"
            or not config.get("language", "en-US").lower().startswith("en")):
        return None
    phones = _pronunciations(words, Path(config["dictionary"]), pronunciations or {})
    if not phones or not (choices := _alternatives(phones[-1])):
        return None
    with TemporaryDirectory(prefix="dmsp-name-audio-") as temp:
        folder = Path(temp)
        wave = folder / "clip.wav"
        _tool_run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(ogg), "-af",
                   "adelay=150,apad=pad_dur=0.25", "-ar", "16000", "-ac", "1", str(wave)], 30)
        dictionary, grammar = folder / "name.dict", folder / "name.jsgf"
        leaders = {"w" + str(i): " ".join(p) for i, p in enumerate(phones[:-1])}
        extra = _extra_syllable(ogg, name_start_sample, phones, config, wave, dictionary, grammar, leaders)
        if extra:
            return extra
        dictionary.write_text("".join(f"{k} {v}\n" for k, v in {**choices, **leaders}.items()), encoding="ascii")
        line = " ".join([*leaders, "(" + " | ".join(choices) + ")"])
        grammar.write_text(f"#JSGF V1.0;\ngrammar name;\npublic <line> = {line};\n", encoding="ascii")
        observed = _recognize(config, dictionary, ["-jsgf", str(grammar)], wave)
        candidate = next((w for w in observed if w["t"] in choices), None)
        if not candidate or not candidate["t"].startswith("l"):
            return None
        hypothesis = choices[candidate["t"]]
        whole = hypothesis in _STOCK
        # A competing short name can lower the grammar score even when the
        # entire stock word is audible. In that range require both searches
        # to locate the complete word at the same boundaries.
        needs_complete_match = hypothesis in _STOCK[:2] and .50 <= candidate["p"] < .60
        if not needs_complete_match and candidate["p"] < (.60 if whole else .75):
            return None
        dictionary.write_text("".join(f"stock{i} {p}\n" for i, p in enumerate(_KEYWORDS)), encoding="ascii")
        keywords = folder / "keywords.txt"
        keywords.write_text("".join(f"stock{i} /1e-12/\n" for i in range(len(_KEYWORDS))), encoding="ascii")
        observed = _recognize(config, dictionary, ["-kws", str(keywords), "-kws_delay", "0"], wave)
        onset = name_start_sample / RATE + .150
        for word in observed:
            if not word["t"].startswith("stock"):
                continue
            fragment = _KEYWORDS[int(word["t"][5:])]
            word_end = word["b"] + word["d"]
            complete_agreement = (
                hypothesis in _STOCK[:2] and fragment in _STOCK[:2] and word["p"] >= .85
                and abs(word["b"] - candidate["b"]) <= .060
                and abs(word_end - (candidate["b"] + candidate["d"])) <= .080
            )
            # A forced alignment to the requested name can place its onset inside
            # a substituted word. Two complete-word decodes must then agree on
            # both boundaries, with the expected onset still inside that word.
            if not onset - .10 <= word["b"] <= onset + .20:
                if not (complete_agreement and candidate["p"] >= .70
                        and word["b"] < onset < word_end - .080):
                    continue
            if fragment == _STOCK_INTERIOR:
                # Connected speech can obscure the initial "ad" even with an
                # intact "venture". Require an independently timed, complete
                # stock-word hypothesis and a distinct intended pronunciation.
                interior_offset = word["b"] - candidate["b"]
                ending_offset = (candidate["b"] + candidate["d"]) - (word["b"] + word["d"])
                if (hypothesis not in _STOCK[:2] or fragment in " ".join(phones[-1])
                        or candidate["p"] < .70 or word["p"] < .85 or word["d"] < .22
                        or not 0 <= interior_offset <= .18 or not -.04 <= ending_offset <= .12
                        or abs(candidate["b"] - onset) > .10):
                    continue
            elif needs_complete_match:
                if not complete_agreement:
                    continue
            elif whole and candidate["p"] < .80 and fragment not in (*_KEYWORDS[:2], *_STOCK[:2]):
                continue
            return dict(hypothesis=hypothesis, word_score=candidate["p"], supporting_phones=fragment,
                        keyword_score=word["p"], keyword_start_seconds=round(word["b"] - .150, 3),
                        keyword_duration_seconds=word["d"], name_start_sample=name_start_sample)
    return None
