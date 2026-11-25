"""Run the optional PocketSphinx adapter. See third_party/pocketsphinx.md."""

from collections.abc import Mapping
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter
import unicodedata

from mfa_phonemes import import_grid
from phoneme_alignment import language_tag, read, write

_PROFILE_KEYS = ('executable', 'acoustic_model', 'dictionary', 'profile_id')
_GRAMMAR_MISS = 'Final result does not match the grammar'
_WIDER_SEARCH = ('-beam', '1e-65', '-pbeam', '1e-65', '-wbeam', '1e-65')
_LEASE_BUSY = 75


class PhonemeAlignmentBusy(ValueError):
    """The shared inference runtime is in use by another operation."""


def _tokens(transcript):
    normalized = unicodedata.normalize('NFC', transcript.replace('’', "'")).lower()
    words, current = [], []
    for index, char in enumerate(normalized):
        category = unicodedata.category(char)[0]
        if category == 'L' or (category == 'M' and current):
            current.append(char)
        elif (char == "'" and current and index + 1 < len(normalized)
              and normalized[index + 1].isalpha()):
            current.append(char)
        else:
            if category in ('M', 'N'):
                raise ValueError('Use spelled-out words for the phoneme alignment transcript.')
            if current:
                words.append(''.join(current))
                current.clear()
    if current:
        words.append(''.join(current))
    if not words:
        raise ValueError('The phoneme alignment transcript has no spoken words.')
    return ' '.join(words)


@lru_cache(maxsize=8)
def _dictionary_words(path, modified_ns, size):
    """Read the same lexicon the aligner will use, including word variants."""
    normalize = re.compile(r'\([1-9][0-9]*\)$')
    tokens = (line.split(maxsplit=1) for line in path.read_text(encoding='utf-8').splitlines())
    return frozenset(unicodedata.normalize('NFC', normalize.sub('', row[0]).lower())
                     for row in tokens if len(row) == 2)


def language_matches(actual, requested):
    """An unspecified region can match a declared one, but sibling regions differ."""
    actual, requested = language_tag(actual), language_tag(requested)
    return not requested or bool(actual and (
        actual == requested or actual.startswith(requested + '-') or requested.startswith(actual + '-')))


def select_profile(parser, language):
    """Prefer an explicit locale, then its generic language, then a compatible default."""
    tag = language_tag(language)
    profiles = {}
    for section in parser.sections():
        prefix, separator, locale = section.partition(':')
        if separator and prefix == 'phoneme_alignment':
            normalized = language_tag(locale)
            if not normalized or normalized in profiles:
                raise ValueError('Phoneme alignment language profiles must have unique locale tags.')
            profiles[normalized] = {**parser[section], 'language': locale.strip()}
    while tag:
        if tag in profiles:
            profile = profiles[tag]
            if not profile.get('phone_set', '').strip():
                raise ValueError('A language-specific phoneme alignment profile needs a phone set.')
            return profile
        tag = tag.rpartition('-')[0]
    if 'phoneme_alignment' in parser:
        default = parser['phoneme_alignment']
        if language_matches(default.get('language', 'en-US'), language):
            return default
    return None


def _tool_run(args, timeout):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError('The local phoneme aligner could not complete.') from error
    if args[0] == 'flock' and result.returncode == _LEASE_BUSY:
        raise PhonemeAlignmentBusy('The shared inference lease is busy.')
    if result.returncode:
        details = result.stderr.strip().splitlines()[-1:] or ['No output from the aligner.']
        raise ValueError('Local phoneme alignment failed. ' + details[0][:240])
    return result


def _alignment_grid(result):
    try:
        words, phones = [], []
        for item in result['w']:
            if item['t'] in ('<sil>', '<s>', '</s>'):
                continue
            label = re.sub(r'\([0-9]+\)$', '', item['t'])
            words.append([item['b'], round(item['b'] + item['d'], 6), label])
            for phone in item['w']:
                phones.append([phone['b'], round(phone['b'] + phone['d'], 6), phone['t']])
    except (KeyError, TypeError) as error:
        raise ValueError('The local aligner returned incomplete word or phoneme data.') from error
    return {'tiers': {'words': {'entries': words}, 'phones': {'entries': phones}}}


def _digest(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def _inference_command(executable, arguments, lease, wait_seconds=0):
    # The shared lease lives outside the project on machines that use one.
    # flock owns the lease for the lifetime of the short-lived CPU process.
    if not lease:
        return [str(executable), *arguments]
    wait = ['-w', str(wait_seconds)] if wait_seconds else ['-n']
    return ['flock', *wait, '-E', str(_LEASE_BUSY), str(lease), str(executable), *arguments]


def align(path, asset, transcript, config, *, wait_seconds=0):
    """Return measured intervals labeled with the installed model's language and notation."""
    if not isinstance(config, Mapping) or any(not config.get(key) for key in _PROFILE_KEYS):
        raise ValueError('Configure the local phoneme alignment profile first.')
    path = Path(path)
    executable, model, dictionary = (Path(config[key]).expanduser()
                                      for key in _PROFILE_KEYS[:3])
    if not executable.is_file() or not model.is_dir() or not dictionary.is_file():
        raise ValueError('The configured phoneme aligner, acoustic model or dictionary is missing.')
    if _digest(path) != asset.get('sha256'):
        raise ValueError('The audio changed before phoneme alignment.')
    text = _tokens(transcript)
    info = dictionary.stat()
    missing = sorted(set(text.split()) - _dictionary_words(dictionary, info.st_mtime_ns, info.st_size))
    if missing:
        raise ValueError('Local phoneme alignment failed. Unknown words: ' + ', '.join(missing))
    with TemporaryDirectory(prefix='dmspeaks-phone-') as folder:
        wave = str(Path(folder) / 'speech.wav')
        _tool_run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-ac', '1',
                   '-ar', '16000', '-c:a', 'pcm_s16le', wave], 120)
        options = ['-hmm', str(model), '-dict', str(dictionary), '-phone_align', 'yes']
        command = ['align', wave, text]
        lease = config.get('inference_lease')
        # Report wall time for the entire leased step. A waiting invocation
        # includes time spent queued before the recognizer starts.
        started = perf_counter()
        attempt = _tool_run(_inference_command(executable, options + command, lease, wait_seconds),
                            120 + wait_seconds)
        widened = not attempt.stdout.strip() and _GRAMMAR_MISS in attempt.stderr
        if widened:
            attempt = _tool_run(
                _inference_command(executable, options + list(_WIDER_SEARCH) + command, lease, wait_seconds),
                120 + wait_seconds)
        leased_seconds = perf_counter() - started
    try:
        result = json.loads(attempt.stdout)
    except (TypeError, ValueError) as error:
        raise ValueError('The local phoneme aligner returned unreadable output.') from error
    if _digest(path) != asset['sha256']:
        raise ValueError('The audio changed during phoneme alignment.')
    evidence = import_grid(_alignment_grid(result), asset, transcript,
                           provider='PocketSphinx ' + config['profile_id'],
                           language=config.get('language', 'en-US'),
                           phone_set=config.get('phone_set', 'ARPAbet'))
    if widened:
        evidence['alignment_search'] = 'wider_after_grammar_miss'
    if wait_seconds:
        evidence['leased_alignment_wall_seconds'] = round(leased_seconds, 3)
    return evidence


def obtain(folder, asset, transcript, *, path, config=None, language=None):
    """Use compatible saved evidence or the configured locale's backend."""
    try:
        evidence = read(folder, asset, transcript)
    except ValueError:
        if not config:
            raise
    else:
        if not language_matches(evidence['language'], language):
            raise ValueError('The saved phoneme alignment uses a different language.')
        return evidence
    if not language_matches(config.get('language', 'en-US'), language):
        raise ValueError('The phoneme alignment profile uses a different language.')
    evidence = align(path, asset, transcript, config)
    write(folder, asset, transcript, evidence)
    return evidence
