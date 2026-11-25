"""Edit an imported entry's delimiter in the project working copy."""
import copy
import json

from delimiter_definitions import (FIELD, INFERRED_COMMENTS_FIELD, LIMIT, NAME, dictionary_definition,
                                   inferred_comments, local_registered, validate_definition)
from project_delimiter_library import read
from project_delimiter_resolution import resolve

ENTRY_AUDIO_ANCHOR = '_dmspeaks_entry_delimiter_audio_revision'


def _semantic_definitions(raw):
    definitions = local_registered({'all_metadata': {FIELD: raw}})
    return [{key: value for key, value in dictionary_definition(item).items() if key not in {'notes', 'choices'}}
            for item in definitions]


def _matching_anchor(fields, field):
    try:
        anchor = json.loads(fields.get(ENTRY_AUDIO_ANCHOR) or 'null')
        if isinstance(anchor, dict) and anchor.get('field') == field and 'value' in anchor:
            if _semantic_definitions(anchor['value']) == _semantic_definitions(fields[field]):
                return anchor
    except (KeyError, TypeError, ValueError):
        pass
    return None


def restore_entry_audio_fields(fields):
    """Restore the exact prior JSON or CSV representation for the audio hash."""
    field = FIELD if FIELD in fields else FIELD + '_json'
    anchor = _matching_anchor(fields, field) if ENTRY_AUDIO_ANCHOR in fields else None
    fields.pop(ENTRY_AUDIO_ANCHOR, None)
    if anchor is not None:
        fields[field] = anchor['value']


def plan_entry_delimiter_storage(kind, row, definitions):
    """Update the definition and its audio anchor in the same history command."""
    field = next((key for key in (FIELD, FIELD + '_json') if key in row), None)
    if field is None:
        field, value = delimiter_storage(kind, definitions)
        return field, value, None
    original = row[field]
    value = json.dumps(definitions, ensure_ascii=False) if isinstance(original, str) or kind == 'csv' else definitions
    prior_anchor = _matching_anchor(row, field)
    anchor = None
    if _semantic_definitions(original) == _semantic_definitions(value):
        previous = prior_anchor or {'field': field, 'value': original}
        if previous['value'] != value:
            anchor = json.dumps(previous, ensure_ascii=False)
    if anchor == row.get(ENTRY_AUDIO_ANCHOR):
        return field, value, None
    operation = 'remove' if anchor is None else 'replace' if ENTRY_AUDIO_ANCHOR in row else 'add'
    change = {'operation': operation, 'pointer': '/' + ENTRY_AUDIO_ANCHOR}
    if anchor is not None:
        change['value'] = anchor
    return field, value, change


def replace_existing_definition(definitions, definition):
    """Return an edit that preserves the identity of the authored marker."""
    candidate = validate_definition(definition)
    prior = next((item for item in definitions if item['key'] == candidate['key']), None)
    if prior is None:
        raise ValueError('Choose an existing entry-specific delimiter.')
    if (candidate['representation'] != prior['representation']
            or candidate.get('type', 'replacement') != prior.get('type', 'replacement')):
        raise ValueError('The delimiter string and type cannot be changed here yet.')
    if candidate['name'] != prior['name'] and not NAME.fullmatch(candidate['name']):
        raise ValueError('Use a delimiter name without spaces.')
    return [candidate if item['key'] == prior['key'] else item for item in definitions]


def delimiter_storage(kind, definitions):
    if kind == 'csv':
        return FIELD + '_json', json.dumps(definitions, ensure_ascii=False)
    return FIELD, copy.deepcopy(definitions)


def update(store, record, definition, expected_revision):
    """Update descriptive properties without changing the source marker.

    Marker and type changes require additional checks of every reference and
    are deliberately not supported by this operation.
    """
    from metadata_editor import edit

    with store.lock:
        current = store.record_structure(record['dataset'], record['index'])
        keys = ('revision', 'metadata_revision', 'structure_revision', 'corpus_revision')
        if any(current[key] != record[key] for key in keys) or expected_revision != current['corpus_revision']:
            raise ValueError('This entry changed. Reopen Delimiter Dictionary.')
        updated = replace_existing_definition(local_registered(current), definition)
        resolve(read(store, current['dataset']), updated)
        data = store.load(current['dataset'])
        if current.get('new_entry'):
            from corpus_structure_commands import read_state, save_added_entry
            snapshot = read_state(store, current['dataset'])['snapshot']
            metadata = copy.deepcopy(snapshot['added_entries'][str(current['index'])]['metadata'])
            field, value, anchor_change = plan_entry_delimiter_storage(data['kind'], metadata, updated)
            metadata[field] = value
            if anchor_change:
                from json_fields import apply_field
                apply_field(metadata, anchor_change)
            result = save_added_entry(store, current['dataset'], current['index'], current['text'], metadata,
                                      expected_structure_revision=current['structure_revision'],
                                      expected_source_revision=current['revision'],
                                      expected_metadata_revision=current['metadata_revision'])
            return {'record': store.record_structure(current['dataset'], current['index']),
                    'history_scope': 'structure', 'revision': result['revision']}
        row = data['rows'][current['index']]
        field, value, anchor_change = plan_entry_delimiter_storage(data['kind'], row, updated)
        result = edit(store, current['dataset'], current['index'], 'dialogue',
                      {'operation': 'replace' if field in row else 'add', 'pointer': '/' + field, 'value': value},
                      expected_revision, include_history_id=True, delimiter_anchor_change=anchor_change,
                      expected_structure={'index': current['index'], 'revision': current['structure_revision']})
        return {'record': store.record_structure(current['dataset'], current['index']),
                'history_id': result['metadata_history_id'], 'history_scope': 'metadata'}


def update_inferred_comment(store, record, key, marker, notes, previous_notes, expected_revision):
    """Keep comments on inferred markers in the dialogue's editorial metadata."""
    from json_fields import apply_field
    from metadata_editor import edit
    if any(not isinstance(value, str) for value in (key, marker, notes, previous_notes)) or len(notes) > 4000:
        raise ValueError('Enter a comment of at most 4,000 characters.')
    dataset, index = record['dataset'], record['index']
    with store.lock:
        current = store.record_structure(dataset, index)
        revisions = ('revision', 'metadata_revision', 'structure_revision', 'corpus_revision')
        if any(current[field] != record[field] for field in revisions) or expected_revision != current['corpus_revision']:
            raise ValueError('This entry changed. Open its comments again.')
        definition = next((item for item in current['delimiters']
                           if item.get('inferred') and item['key'] == key and item['representation'] == marker), None)
        if not definition or (definition['notes'] or '') != previous_notes:
            raise ValueError('This delimiter or its comments changed. Open its comments again.')
        if notes == previous_notes:
            return {'record': current}
        if current['new_entry']:
            from corpus_structure_commands import read_state, save_added_entry
            snapshot = read_state(store, dataset)['snapshot']
            row = copy.deepcopy(snapshot['added_entries'][str(index)]['metadata'])
        else:
            row = store.load(dataset)['rows'][index]
        comments = inferred_comments(row)
        if key not in comments and len(comments) >= LIMIT:
            raise ValueError('This dialogue has reached its delimiter comment limit.')
        # An explicit empty comment can hide a description inherited from the corpus.
        comments[key] = notes
        change = {'operation': 'replace' if INFERRED_COMMENTS_FIELD in row else 'add',
                  'pointer': '/' + INFERRED_COMMENTS_FIELD, 'value': json.dumps(comments, ensure_ascii=False)}
        if current['new_entry']:
            apply_field(row, change)
            result = save_added_entry(store, dataset, index, current['text'], row,
                                      expected_structure_revision=current['structure_revision'],
                                      expected_source_revision=current['revision'],
                                      expected_metadata_revision=current['metadata_revision'])
            return {'record': store.record_structure(dataset, index), 'history_scope': 'structure',
                    'revision': result['revision']}
        result = edit(store, dataset, index, 'dialogue', change, expected_revision, include_history_id=True,
                      expected_structure={'index': index, 'revision': current['structure_revision']},
                      inferred_comment=True)
        return {'record': store.record_structure(dataset, index), 'history_id': result['metadata_history_id'],
                'history_scope': 'metadata'}
