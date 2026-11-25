"""Import Montreal Forced Aligner word/phone tiers for an exact recording."""

import argparse
import hashlib
import json
from pathlib import Path
import unicodedata

from guide_bindings import RATE
from phoneme_alignment import FORMAT, transcript_digest, validate, word_offsets, write

SILENCE = {'', 'sil', '<sil>', 'sp'}


def _tier(grid, name):
    tiers = grid.get('tiers')
    if isinstance(tiers, dict):
        entries = tiers.get(name, {}).get('entries')
    else:
        matches = [tier for tier in tiers or [] if tier.get('name') == name]
        entries = matches[0].get('entries') if len(matches) == 1 else None
    if not isinstance(entries, list):
        raise ValueError(f'The alignment needs one {name} interval tier.')
    return entries


def _entries(grid, name, duration):
    previous = 0
    for start, end, text in _tier(grid, name):
        if (type(start) not in (float, int) or type(end) not in (float, int)
                or not 0 <= start < end <= duration + 1 / RATE
                or start < previous or not isinstance(text, str)):
            raise ValueError(f'The {name} tier contains invalid times or labels.')
        previous = end
        if text.strip().casefold() not in SILENCE:
            begin, stop = round(start * RATE), min(round(end * RATE), round(duration * RATE))
            if begin >= stop:
                raise ValueError(f'The {name} tier contains an empty interval.')
            yield begin, stop, unicodedata.normalize('NFC', text.strip())


def import_grid(grid, asset, transcript, *, language, phone_set, provider):
    duration = asset['sample_count'] / RATE
    word_rows = list(_entries(grid, 'words', duration))
    phones = iter(_entries(grid, 'phones', duration))
    phone = next(phones, None)
    words = []
    for (start, end, label), (left, right) in zip(
            word_rows, word_offsets(transcript, [row[2] for row in word_rows]), strict=True):
        aligned = []
        while phone is not None and phone[0] < end:
            begin, stop, symbol = phone
            if not start <= begin < stop <= end:
                raise ValueError('An MFA phone crosses its word interval.')
            aligned.append({'phone': symbol, 'start_sample': begin, 'end_sample': stop})
            phone = next(phones, None)
        words.append({'word': transcript[left:right], 'char_start': left, 'char_end': right,
                      'start_sample': start, 'end_sample': end, 'phonemes': aligned})
    if phone is not None:
        raise ValueError('An MFA phone is outside its word interval.')
    evidence = {'format': FORMAT, 'audio_sha256': asset['sha256'], 'sample_rate': RATE,
                'sample_count': asset['sample_count'], 'transcript_sha256': transcript_digest(transcript),
                'language': language, 'phone_set': phone_set, 'provider': provider, 'words': words}
    validate(evidence, asset, transcript)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('store', type=Path, help='DMSpeaks working project folder')
    parser.add_argument('audio', type=Path, help='Exact saved 44.1 kHz mono recording')
    parser.add_argument('transcript', type=Path, help='Exact UTF-8 spoken transcript')
    parser.add_argument('alignment', type=Path, help='MFA alignment JSON')
    parser.add_argument('--language', required=True)
    parser.add_argument('--phone-set', required=True)
    parser.add_argument('--provider', required=True, help='Aligner and language model version')
    args = parser.parse_args()
    from layered_audio import LayerSourceCache
    from fish_community_tts import probe

    media = probe(args.audio)
    if media['sample_rate'] != RATE or media['channels'] != 1:
        parser.error('the exact saved recording must be mono at 44.1 kHz')
    with args.audio.open('rb') as file:
        digest = hashlib.file_digest(file, 'sha256').hexdigest()
    samples, channels = LayerSourceCache().read(args.audio)
    asset = {'sha256': digest, 'sample_rate': RATE, 'sample_count': len(samples) // channels}
    transcript = args.transcript.read_text(encoding='utf-8')
    evidence = import_grid(json.loads(args.alignment.read_text(encoding='utf-8')), asset, transcript,
                           language=args.language, phone_set=args.phone_set, provider=args.provider)
    with args.audio.open('rb') as file:
        if hashlib.file_digest(file, 'sha256').hexdigest() != digest:
            parser.error('the recording changed during import')
    print(write(args.store, asset, transcript, evidence))


if __name__ == '__main__':
    main()
