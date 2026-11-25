"""Validation and project persistence for the future corpus entry structure.

Entry editing is not enabled by this module. It keeps the source binding and
stable slot assignments intact when a project is saved or reopened.
"""
import hashlib
import json

from corpus_structure_slots import validate as validate_slots


def source_binding(original, mapping, row_count):
    digest = hashlib.sha256(original + json.dumps(mapping, sort_keys=True).encode()).hexdigest()
    return digest, row_count


def validate(snapshot, original, mapping, row_count):
    if not isinstance(snapshot, dict) or set(snapshot) != {
        'version', 'source_id', 'original_count', 'state', 'added_entries'
    }:
        raise ValueError('Invalid project entry structure.')
    source_id, original_count = source_binding(original, mapping, row_count)
    if (type(snapshot['version']) is not int or snapshot['version'] != 1 or
        snapshot['source_id'] != source_id or type(snapshot['original_count']) is not int or
        snapshot['original_count'] != original_count):
        raise ValueError('Entry structure belongs to another corpus.')
    state = validate_slots(snapshot['state'])
    entries = snapshot['added_entries']
    if state['next_slot'] < original_count or not isinstance(entries, dict):
        raise ValueError('Invalid project entry structure.')
    if len(entries) != state['next_slot'] - original_count:
        raise ValueError('Added entries do not match allocated slots.')
    for slot in range(original_count, state['next_slot']):
        content = entries.get(str(slot))
        if (not isinstance(content, dict) or not isinstance(content.get('text'), str) or
            len(content['text']) > 50000 or not isinstance(content.get('metadata'), dict)):
            raise ValueError('Invalid added entry.')
    try:
        # Return detached, finite JSON rather than caller-owned objects.
        return json.loads(json.dumps(snapshot, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid project entry structure.') from exc


def setting_key(dataset):
    return 'corpus_entry_structure:' + dataset


def read(store, dataset):
    value = store.setting(setting_key(dataset))
    if value is None:
        return None
    data = store.original_load(dataset)
    original = (store.folder / (dataset + '.original')).read_bytes()
    checked = validate(value, original, data['mapping'], len(data['rows']))
    if checked['source_id'][:20] != dataset:
        raise ValueError('The original corpus no longer matches this project.')
    return checked
