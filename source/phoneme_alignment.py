"""Validate measured phone intervals on the decoded recording's sample clock."""

import hashlib
import re
import unicodedata

from guide_bindings import RATE

FORMAT = 'dmspeaks-phoneme-alignment-1'
UNKNOWN_PHONES = {'<unk>', 'spn', '<eps>', 'unk'}


def language_tag(value):
    """Normalize a locale for profile lookup while retaining script and region."""
    tag = str(value or '').strip().replace('_', '-')
    tag = re.sub(r'^([a-z]{2,3})([A-Z]{2})$', r'\1-\2', tag).casefold()
    return {'english': 'en', 'enus': 'en-us'}.get(tag, tag)


def transcript_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def lexical_text(text):
    """Compare labels while preserving letters, numbers and combining marks."""
    return ''.join(char for char in unicodedata.normalize('NFC', text).casefold()
                   if unicodedata.category(char)[0] in 'LMN')


def word_offsets(transcript, labels):
    """Map the emitted tokens back to source characters, including joined scripts."""
    # NFC clusters keep combining marks tied to their original character span.
    positions = []
    index = 0
    while index < len(transcript):
        end = index + 1
        while end < len(transcript) and unicodedata.category(transcript[end]).startswith('M'):
            end += 1
        positions.extend((char, index, end) for char in lexical_text(transcript[index:end]))
        index = end
    compact = ''.join(char for char, _, _ in positions)
    offset = 0
    for label in labels:
        normalized = lexical_text(label)
        if not normalized or not compact.startswith(normalized, offset):
            raise ValueError(f'Aligned word {label!r} differs from the alignment transcript.')
        end = offset + len(normalized)
        yield positions[offset][1], positions[end - 1][2]
        offset = end
    if offset != len(compact):
        raise ValueError('Alignment did not cover the complete transcript.')


def _span(item, limit):
    start, end = item.get('start_sample'), item.get('end_sample')
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= limit:
        raise ValueError('A phoneme alignment interval is outside the decoded recording.')
    return start, end


def validate(evidence, asset, transcript):
    if (not isinstance(evidence, dict) or evidence.get('format') != FORMAT
            or evidence.get('audio_sha256') != asset.get('sha256')
            or re.fullmatch(r'[0-9a-f]{64}', str(evidence.get('audio_sha256'))) is None
            or evidence.get('transcript_sha256') != transcript_digest(transcript)
            or evidence.get('sample_rate') != RATE or asset.get('sample_rate') != RATE
            or evidence.get('sample_count') != asset.get('sample_count')):
        raise ValueError('Phoneme evidence belongs to a different recording, transcript or sample clock.')
    for field in ('provider', 'language', 'phone_set'):
        value = evidence.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 160:
            raise ValueError('Phoneme evidence needs an alignment provider, language and phone set.')

    words = evidence.get('words')
    if not isinstance(words, list) or not words:
        raise ValueError('Phoneme evidence has no timed words.')
    position = previous_end = 0
    for word in words:
        if not isinstance(word, dict):
            raise ValueError('Invalid phoneme word interval.')
        start, end = _span(word, asset['sample_count'])
        left, right = word.get('char_start'), word.get('char_end')
        if (type(left) is not int or type(right) is not int
                or not position <= left < right <= len(transcript)
                or lexical_text(transcript[position:left]) or start < previous_end
                or not isinstance(word.get('word'), str) or not lexical_text(word['word'])
                or lexical_text(transcript[left:right]) != lexical_text(word['word'])):
            raise ValueError('Phoneme word intervals differ from the transcript or overlap.')
        phones = word.get('phonemes')
        if not isinstance(phones, list):
            raise ValueError('Invalid phoneme intervals for a word.')
        prior_phone = start
        for phone in phones:
            if not isinstance(phone, dict):
                raise ValueError('Invalid phoneme interval.')
            begin, stop = _span(phone, end)
            symbol = phone.get('phone')
            if (begin < prior_phone or not isinstance(symbol, str)
                    or not symbol.strip() or len(symbol) > 40):
                raise ValueError('Phoneme intervals overlap or have invalid labels.')
            prior_phone = stop
        position, previous_end = right, end
    if lexical_text(transcript[position:]):
        raise ValueError('Phoneme evidence is missing words from the transcript.')
    return words


def timeline_words(evidence, words, *, origin=0):
    """Give the word rail and its phone rail the same media clock."""
    return [
        {'word': word['word'], 'start': (word['start_sample'] - origin) / RATE,
         'end': (word['end_sample'] - origin) / RATE,
         'phonemes': [
             {'phone': phone['phone'], 'start': (phone['start_sample'] - origin) / RATE,
              'end': (phone['end_sample'] - origin) / RATE,
              'provider': evidence['provider'], 'language': evidence['language'],
              'phone_set': evidence['phone_set']}
             for phone in word['phonemes'] if phone['phone'] not in UNKNOWN_PHONES
         ]}
        for word in words
    ]


def position_phones(phones, *, start, end, trim, speed, offset):
    """Project media-clock phones through the same clip transform as words."""
    result = []
    for phone in phones:
        lo = max(start, (phone['start'] - trim) / speed - offset)
        hi = min(end, (phone['end'] - trim) / speed - offset)
        if round(lo * RATE) < round(hi * RATE):
            result.append({**phone, 'start': lo, 'end': hi})
    return result


def evidence_path(folder, asset, transcript):
    from pathlib import Path

    digest = asset.get('sha256')
    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('The recording has no verified audio identity.')
    return Path(folder) / 'phoneme-alignments' / f'{digest}-{transcript_digest(transcript)}.json'


def read(folder, asset, transcript):
    import json

    path = evidence_path(folder, asset, transcript)
    try:
        with path.open('rb') as file:
            raw = file.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('The phoneme evidence exceeds the size limit.')
        evidence = json.loads(raw)
    except FileNotFoundError:
        raise ValueError('This recording has no saved phoneme alignment.') from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError('The saved phoneme alignment could not be read.') from None
    validate(evidence, asset, transcript)
    return evidence


def write(folder, asset, transcript, evidence):
    import json
    from tempfile import NamedTemporaryFile
    from pathlib import Path

    validate(evidence, asset, transcript)
    path = evidence_path(folder, asset, transcript)
    raw = json.dumps(evidence, ensure_ascii=False, indent=2).encode('utf-8')
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('The phoneme evidence exceeds the size limit.')
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(raw)
            file.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def for_asset(store, asset):
    """Read portable or locally cached evidence for a source take."""
    transcript = (asset.get('generation') or {}).get('spoken_text')
    if not isinstance(transcript, str) or not transcript:
        return None
    try:
        evidence = asset.get('phoneme_alignment')
        if evidence is None and getattr(store, 'folder', None) is not None:
            evidence = read(store.folder, asset, transcript)
        if evidence is not None:
            validate(evidence, asset, transcript)
        return evidence
    except ValueError:
        return None


def with_timeline_words(store, asset):
    """Add the measured word and phone clock without replacing saved provider data."""
    existing = asset.get('words') or []
    if any(word.get('phonemes') for word in existing):
        return asset
    evidence = for_asset(store, asset)
    if evidence is None:
        return asset
    measured = timeline_words(evidence, evidence['words'])
    if existing:
        if [lexical_text(word['word']) for word in existing] != [
                lexical_text(word['word']) for word in measured]:
            return asset
        measured = [{**old, **new} for old, new in zip(existing, measured)]
    return {**asset, 'words': measured, 'timing_source': 'estimated_phoneme_alignment'}

def bind_transcript(evidence, asset, old, new):
    """Relabel the same aligned phones for equivalent punctuation and casing."""
    if old == new:
        return evidence
    validate(evidence, asset, old)
    if lexical_text(old) != lexical_text(new):
        raise ValueError('Phoneme evidence cannot be reused for a different spoken transcript.')
    words = evidence['words']
    offsets = word_offsets(new, [word['word'] for word in words])
    rebound = {**evidence, 'transcript_sha256': transcript_digest(new),
               'words': [{**word, 'word': new[left:right], 'char_start': left, 'char_end': right}
                         for word, (left, right) in zip(words, offsets, strict=True)]}
    validate(rebound, asset, new)
    return rebound
