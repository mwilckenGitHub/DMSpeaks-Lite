"""Internal standalone corpus export for stable entry slots.

This exports text and corpus metadata, not a portable project or approved audio.
The normal UI does not expose structural editing or this export yet.
"""
import copy
import csv
import io
import json

from clip_group_corpus import EXTENSION as GROUP_FIELD, embed as embed_groups
from corpus_lookup import EXTENSION as LOOKUP_FIELD
from audio_sources import EXTENSION as AUDIO_FIELD
from corpus_structure_commands import read_state
from model import decode_corpus, delimiter_pattern, flatten, getfield, setfield


def export(store, dataset):
    """Export visible entries and return an explicit source-to-output slot map."""
    from entry_delimiter_edit import ENTRY_AUDIO_ANCHOR
    with store.lock:
        own_transaction = not store.db.in_transaction
        if own_transaction:
            store.db.execute('BEGIN')
        try:
            structure = read_state(store, dataset)
            snapshot = structure['snapshot']
            data = store.load(dataset)
            # Added entries do not exist in the baseline's row array. Embed
            # their groups only after constructing and reindexing that array.
            raw, media_type, name = store.export(dataset, include_group_state=False, _include_structure=False)
            baseline = decode_corpus(raw, name)
            baseline['mapping'] = copy.deepcopy(data['mapping'])
            order = snapshot['state']['order']
            if not order:
                raise ValueError('A corpus export needs at least one visible entry.')
            original_count = snapshot['original_count']
            rows = []
            from project_delimiter_library import read as read_project_delimiters
            definitions = read_project_delimiters(store, dataset)
            drafts = store.draft_map(dataset)
            for slot in order:
                if slot < original_count:
                    row = copy.deepcopy(baseline['rows'][slot])
                else:
                    entry = snapshot['added_entries'][str(slot)]
                    text = drafts.get(slot, entry['text'])
                    row = copy.deepcopy(entry['metadata'])
                    row.pop(ENTRY_AUDIO_ANCHOR, None)
                    if data['mapping']['text'] == '@segments':
                        store._apply_export_text(data, row, text, definitions)
                    else:
                        setfield(row, data['mapping']['text'], text)
                    _attach_definitions(row, text, definitions, data['kind'])
                if data['kind'] == 'csv' and any(isinstance(value, (dict, list)) for value in row.values()):
                    raise ValueError('CSV entry metadata must contain scalar values.')
                rows.append(row)
            root = copy.deepcopy(baseline['root'])
            if data['container'] is None:
                # Array/CSV corpus-wide extensions are stored on the first row.
                # Move the original extension values with the corpus, not the row.
                keys = [field + ('_json' if data['kind'] == 'csv' else '')
                        for field in (LOOKUP_FIELD, AUDIO_FIELD, GROUP_FIELD)]
                extensions = {key: copy.deepcopy(baseline['rows'][0][key])
                              for key in keys if key in baseline['rows'][0]}
                if 0 in order:
                    old_first = rows[order.index(0)]
                    for key in extensions:
                        old_first.pop(key, None)
                for key in keys:
                    # CSV can contain empty cells in every row for a reserved
                    # column. JSON key presence is meaningful even when null.
                    occupied = key in rows[0] and (data['kind'] != 'csv' or rows[0][key] not in (None, ''))
                    if occupied:
                        raise ValueError('An entry conflicts with reserved corpus metadata: ' + key)
                    if key in extensions:
                        rows[0][key] = extensions[key]
                root = rows
            else:
                root[data['container']] = rows
                # Spoken corpus explicitly declares this count; other unknown
                # metadata is preserved without guessing its meaning.
                if {'schemaVersion','lineCount','npcs','spawns'} <= root.keys():
                    root['lineCount'] = len(rows)

            from clip_groups import export_config
            groups = copy.deepcopy(export_config(store,dataset))
            target_for_slot = {slot: index for index, slot in enumerate(order)}
            remapped = []
            for item in groups['dialogues']:
                if item['index'] not in target_for_slot:
                    continue
                target = target_for_slot[item['index']]
                entry_id = getfield(rows[target], data['mapping'].get('id'))
                item['index'] = target
                item['dialogue_id'] = entry_id if entry_id not in (None, '') else f'row-{target + 1}'
                remapped.append(item)
            groups['dialogues'] = sorted(remapped, key=lambda item: item['index'])
            # Remove only the previously validated group extension, whose old
            # positions are no longer valid. The embedder checks new collisions.
            group_key = GROUP_FIELD + ('_json' if data['kind'] == 'csv' else '')
            owner = root if isinstance(root, dict) else root[0]
            owner.pop(group_key, None)
            embed_groups(root, groups, data['kind'], data['container'])
            if data['kind'] == 'csv':
                buffer = io.StringIO(newline='')
                fields = list(dict.fromkeys([*data['fields'], *(key for row in rows for key in row)]))
                writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator='\r\n')
                writer.writeheader(); writer.writerows(rows)
                output = buffer.getvalue().encode('utf-8')
            else:
                output = json.dumps(root, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
            # Verify the resulting format and group identities using real import rules.
            checked = decode_corpus(output, name)
            for item in checked['clip_group_config']['dialogues']:
                entry_id = getfield(checked['rows'][item['index']], data['mapping'].get('id'))
                actual = entry_id if entry_id not in (None, '') else f"row-{item['index'] + 1}"
                if item['dialogue_id'] != actual:
                    raise ValueError('A replacement group does not match its exported entry.')
            result = {'data': output, 'media_type': media_type, 'name': name,
                      'structure_revision': structure['revision'],
                      'source_mapping': copy.deepcopy(data['mapping']),
                      'slot_map': [{'source_slot': slot, 'output_index': index}
                                   for index, slot in enumerate(order)],
                      'audio_reuse_approved': False}
            if own_transaction:
                store.db.commit()
            return result
        except BaseException:
            if own_transaction:
                store.db.rollback()
            raise


def _attach_definitions(row, text, definitions, kind):
    if not definitions:
        return
    from delimiter_definitions import FIELD, dictionary_definition, local_registered
    from project_delimiter_resolution import resolve
    used = {match[0] for match in delimiter_pattern(definitions).finditer(text)}
    relevant = [item for item in definitions if item['representation'] in used]
    if not relevant:
        return
    merged = [dictionary_definition(item) for item in resolve(relevant, local_registered({'all_metadata': flatten(row)}))]
    row[FIELD + ('_json' if kind == 'csv' else '')] = (
        json.dumps(merged, ensure_ascii=False) if kind == 'csv' else merged)
