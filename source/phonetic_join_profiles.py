"""Look up the acoustic landmarks a phonetic join should preserve."""

from functools import lru_cache
import json
from pathlib import Path
import unicodedata

from phoneme_alignment import language_tag

_PROFILES = Path(__file__).with_name('formats') / 'phonetic-join-profiles-1.json'
_PHONE_SETS = {'cmu': 'arpabet'}
_SPEECH_EDGE = {
    'manner': 'utterance boundary',
    'onset': 'measured quiet before the following speech',
    'release': 'measured quiet after the preceding speech',
}


@lru_cache(maxsize=4)
def load_profiles(path=_PROFILES):
    """Index exact model labels by locale and notation."""
    document = json.loads(path.read_text(encoding='utf-8'))
    if document['format'] != 'dmspeaks-phonetic-join-profiles-1':
        raise ValueError('Unrecognized phonetic join profile format.')
    profiles = {}
    for profile in document['profiles']:
        for notation, inventory in profile['inventories'].items():
            key = (language_tag(profile['language']), notation.casefold())
            if key in profiles:
                raise ValueError(f'Duplicate phonetic join inventory for {key!r}.')
            labels = {}
            for group, spellings in inventory.items():
                description = profile['groups'][group]
                for spelling in spellings:
                    label = unicodedata.normalize('NFC', spelling)
                    if label in labels:
                        raise ValueError(f'Duplicate phone {label!r} in {key!r}.')
                    labels[label] = (group, description)
            profiles[key] = (profile['id'], labels, profile.get('transitions', {}))
    return profiles


def join_rule(left, right, *, language, phone_set, profiles=None):
    """Describe the outgoing and incoming sounds for a measured phone pair."""
    profiles = load_profiles() if profiles is None else profiles
    tag = language_tag(language)
    notation = phone_set.strip().casefold()
    notation = _PHONE_SETS.get(notation, notation)
    while tag and (tag, notation) not in profiles:
        tag = tag.rpartition('-')[0]
    if not tag:
        return {'status': 'no_language_profile'}
    profile, inventory, transitions = profiles[tag, notation]

    def lookup(phone):
        if phone is None:
            return 'utterance_boundary', _SPEECH_EDGE
        phone = unicodedata.normalize('NFC', phone)
        if notation == 'arpabet' and phone[-1:] in ('0', '1', '2'):
            plain = inventory.get(phone[:-1])
            if plain and plain[0] == 'vowel':
                return plain
        return inventory.get(phone)

    outgoing, incoming = lookup(left), lookup(right)
    if outgoing is None or incoming is None:
        return {'status': 'unmapped_phone', 'profile': profile}
    left_group, left_rule = outgoing
    right_group, right_rule = incoming
    left_family = left_rule.get('acoustic_family', left_group)
    right_family = right_rule.get('acoustic_family', right_group)
    transition = next((transitions[key] for key in (
        f'{left_group} {right_group}', f'{left_group} {right_family}',
        f'{left_family} {right_group}', f'{left_family} {right_family}',
    ) if key in transitions), 'check_aligned_transition')
    return {
        'status': 'mapped',
        'profile': profile,
        'outgoing_manner': left_rule['manner'],
        'incoming_manner': right_rule['manner'],
        'outgoing_landmark': left_rule['release'],
        'incoming_landmark': right_rule['onset'],
        'coarticulation': transition,
    }
