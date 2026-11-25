"""Project-wide delimiters, collision checks, and reversible edits.

Imported dialogue-owned definitions retain their own source scope.
"""
from project_delimiter_resolution import resolve
from delimiter_definitions import CHOICE_REVISION_ANCHOR, COMMENT_REVISION_ANCHOR, dictionary_definition


def validate(definitions):
    """Return normalized definitions, rejecting malformed or ambiguous input."""
    return resolve(definitions, [])


def _uses_builtin_speech(definition):
    from model import TOKEN, token_value

    marker = definition['representation']
    return (definition.get('type', 'replacement') == 'replacement'
            and not definition['default'] and TOKEN.fullmatch(marker)
            and token_value(marker, {})[0] == definition['key'])


def audio_definition(definition):
    """Keep the metadata bytes used to bind existing audio when speech is unchanged."""
    fields = dictionary_definition(definition)
    fields.pop('snap_mode', None)
    if COMMENT_REVISION_ANCHOR in definition:
        fields['notes'] = definition[COMMENT_REVISION_ANCHOR]
    if CHOICE_REVISION_ANCHOR in definition:
        choices = definition[CHOICE_REVISION_ANCHOR]
        if choices is None:
            if _uses_builtin_speech(definition):
                return None
        else:
            fields['choices'] = choices
    return fields


def preserve_audio_revision(before, after):
    old_by_marker = {(item['key'], item['representation']): item for item in before}
    for item in after:
        prior = old_by_marker.get((item['key'], item['representation']))
        item.pop(COMMENT_REVISION_ANCHOR, None)
        item.pop(CHOICE_REVISION_ANCHOR, None)
        if prior is not None:
            prior_notes = prior.get(COMMENT_REVISION_ANCHOR, prior['notes'])
            if item['notes'] != prior_notes:
                item[COMMENT_REVISION_ANCHOR] = prior_notes
            prior_choices = prior.get(CHOICE_REVISION_ANCHOR, prior['choices'])
            if prior_choices is None and not _uses_builtin_speech(item):
                prior_choices = prior['choices']
            if item['choices'] != prior_choices:
                item[CHOICE_REVISION_ANCHOR] = prior_choices
        elif _uses_builtin_speech(item):
            # This marker already resolved identically without a definition.
            item[CHOICE_REVISION_ANCHOR] = None
    return after


def plan_import(saved, incoming):
    """Classify an import without modifying either collection."""
    saved = validate(saved)
    incoming = [dictionary_definition(item) for item in validate(incoming)]
    additions, conflicts, skipped = [], [], 0
    for item in incoming:
        identical = next((old for old in saved if dictionary_definition(old) == item), None)
        if identical:
            skipped += 1
            continue
        matches = []
        for old in saved:
            reasons = []
            if old['key'] == item['key']: reasons.append('key')
            if old['name'].casefold() == item['name'].casefold(): reasons.append('name')
            if old['representation'] == item['representation']: reasons.append('marker')
            if reasons: matches.append({'definition': old, 'reasons': reasons})
        if matches: conflicts.append({'incoming': item, 'matches': matches})
        else: additions.append(item)
    return {'additions': additions, 'conflicts': conflicts, 'skipped': skipped}


def setting_key(dataset):
    return 'project_delimiters:' + dataset


def read(store, dataset):
    store.original_load(dataset)
    return validate(store.setting(setting_key(dataset), []))


def write(store, dataset, definitions):
    store.original_load(dataset)
    normalized = validate(definitions)
    store.set_setting(setting_key(dataset), normalized)
    return normalized


def _entries(store, dataset, text_overrides=None):
    """Yield original and retained added entries, including hidden entries.

    Hidden entries remain undoable, so their markers must continue to protect
    project definitions. No structural feature is exposed by this helper.
    """
    from corpus_structure_snapshot import read as read_structure
    from model import getfield
    data = store.load(dataset)
    drafts = store.draft_map(dataset)
    if text_overrides:
        drafts.update(text_overrides)
    for index, row in enumerate(data['rows']):
        text = drafts.get(index)
        if text is None:
            text = (''.join(str(row.get(key) or '') for key in data['slots'])
                    if data['mapping']['text'] == '@segments'
                    else str(getfield(row, data['mapping']['text'], '') or ''))
        yield index, row, text
    structure = read_structure(store, dataset)
    if structure is not None:
        for index in range(structure['original_count'], structure['state']['next_slot']):
            entry = structure['added_entries'][str(index)]
            yield index, entry['metadata'], drafts.get(index, entry['text'])


def source_markers(store, dataset, definitions):
    """Group observed markers and count occurrences in visible entries."""
    from collections import Counter
    from corpus_structure_snapshot import read as read_structure
    from delimiter_definitions import FIELD, KEY, local_registered
    from model import delimiter_pattern, flatten, token_value

    markers = {}
    counts = Counter()
    structure = read_structure(store, dataset)
    visible = set(structure['state']['order']) if structure is not None else None
    shared_pattern = delimiter_pattern(definitions)
    shared_keys = {item['representation']: item['key'] for item in definitions}
    for index, row, text in _entries(store, dataset):
        if FIELD not in row and FIELD + '_json' not in row and not shared_pattern.search(text):
            continue
        local = local_registered({'all_metadata': flatten(row)})
        if local:
            effective = resolve(definitions, local)
            pattern = delimiter_pattern(effective)
            keys = {item['representation']: item['key'] for item in effective}
        else:
            pattern, keys = shared_pattern, shared_keys
        for match in pattern.finditer(text):
            marker = match[0]
            key = keys.get(marker) or token_value(marker, {})[0]
            if KEY.fullmatch(key):
                markers.setdefault(key, set()).add(marker)
                if visible is None or index in visible:
                    counts[key] += 1
    return [{'key': key, 'markers': sorted(choices), 'count': counts[key]}
            for key, choices in sorted(markers.items())]


def validate_new_source_markers(store, dataset, before, after, definitions=None):
    """Reject newly authored placeholders outside the corpus dictionary."""
    from collections import Counter
    from model import TOKEN, delimiter_pattern

    if before == after:
        return
    shared = read(store, dataset) if definitions is None else definitions
    pattern = delimiter_pattern(shared)
    old = Counter(match[0] for match in pattern.finditer(before))
    additions = Counter(match[0] for match in pattern.finditer(after)) - old
    defined = {definition['representation'] for definition in shared}
    unresolved = {marker for marker in additions if TOKEN.fullmatch(marker) and marker not in defined}
    if not unresolved:
        return
    # Imported source markers appear in the corpus dictionary even before a
    # shared fallback is authored. Keep their exact spellings available.
    observed = {marker for item in source_markers(store, dataset, shared) for marker in item['markers']}
    missing = sorted(unresolved - observed)
    if missing:
        raise ValueError(f'Add {missing[0]} under Delimiters → Definitions before inserting it into dialogue.')


def delimiter_use_page(store, dataset, key, offset, limit):
    """List entries with exact source uses of a delimiter, in corpus order."""
    from corpus_structure_commands import public_state, read_state
    from delimiter_definitions import KEY, local_registered
    from model import TOKEN, delimiter_pattern, flatten, token_value

    if not KEY.fullmatch(key) or offset < 0 or not 1 <= limit <= 50:
        raise ValueError('Choose a valid delimiter and result page.')
    definitions = read(store, dataset)
    order = public_state(read_state(store, dataset))['order']
    positions = {index: position for position, index in enumerate(order)}
    matches = []
    shared_pattern = delimiter_pattern(definitions)
    shared_keys = {item['representation']: item['key'] for item in definitions}
    for index, row, text in _entries(store, dataset):
        if index not in positions:
            continue
        local = local_registered({'all_metadata': flatten(row)})
        if local:
            effective = resolve(definitions, local)
            pattern = delimiter_pattern(effective)
            keys = {item['representation']: item['key'] for item in effective}
        else:
            pattern, keys = shared_pattern, shared_keys
        first = None
        count = 0
        for token in pattern.finditer(text):
            marker = token[0]
            token_key = keys.get(marker) or token_value(marker, {})[0]
            if token_key == key:
                if first is None:
                    first = token
                count += 1
        if first is not None:
            matches.append((positions[index], index, first[0], count, text[max(0, first.start()-24):first.end()+48]))
    matches.sort()
    results = []
    for _, index, marker, count, context in matches[offset:offset + limit]:
        record = store.record_structure(dataset, index)
        metadata = record['metadata']
        label = metadata.get('quest_title') or metadata.get('npc_name') or record.get('id') or f'Entry {index+1}'
        results.append({'index': index, 'label': str(label), 'marker': marker,
                        'count': count, 'context': ' '.join(context.split())})
    return {'total': len(matches), 'results': results}


def source_conflicts(store, dataset, definitions, *, text_overrides=None):
    """Find conflicts with existing authored definitions and used source tokens.

    This is read-only. It inspects drafts as well as imported text and never
    guesses a relationship between different keys or markers.
    """
    from delimiter_definitions import KEY, definitions_for_record, registered
    from model import flatten

    project = validate(definitions)
    conflicts = []
    for index, row, text in _entries(store, dataset, text_overrides):
        record = {'text': text, 'all_metadata': flatten(row)}
        local = registered(record)
        inferred = [item for item in definitions_for_record(record) if item.get('inferred')]
        for shared in project:
            for existing in local:
                if existing['key'] == shared['key'] and existing['representation'] != shared['representation']:
                    conflicts.append((index, shared['key'], 'key', existing['representation']))
                elif existing['key'] != shared['key'] and existing['representation'] == shared['representation']:
                    conflicts.append((index, shared['key'], 'marker', existing['key']))
                elif existing['key'] != shared['key'] and existing['name'].casefold() == shared['name'].casefold():
                    conflicts.append((index, shared['key'], 'name', existing['key']))
            for existing in inferred:
                if not KEY.fullmatch(existing['key']) and not any(
                    key.startswith('variables.' + existing['key'] + '.') for key in record['all_metadata']
                ):
                    continue
                # An inferred default spelling alone is not proof it occurs.
                if existing['representation'] not in text:
                    continue
                if existing['key'] == shared['key'] and existing['representation'] != shared['representation']:
                    conflicts.append((index, shared['key'], 'key', existing['representation']))
                elif existing['key'] != shared['key'] and existing['representation'] == shared['representation']:
                    conflicts.append((index, shared['key'], 'marker', existing['key']))
    return conflicts


def used_in_entries(store, dataset, marker, definitions):
    """Return entry indexes where a project marker is actually tokenized."""
    from delimiter_definitions import local_registered
    from model import delimiter_pattern, flatten
    used = []
    for index, row, text in _entries(store, dataset):
        if marker not in text:
            continue
        effective = resolve(definitions, local_registered({'all_metadata': flatten(row)}))
        if any(match[0] == marker for match in delimiter_pattern(effective).finditer(text)):
            used.append(index)
    return used


def marker_use_fingerprints(store, dataset, markers, definitions):
    """Record text identity for specified marker uses in one corpus pass."""
    import hashlib
    from delimiter_definitions import local_registered
    from model import delimiter_pattern, flatten
    result = {marker: {} for marker in markers}
    if not result:
        return result
    for index, row, text in _entries(store, dataset):
        if not any(marker in text for marker in result):
            continue
        effective = resolve(definitions, local_registered({'all_metadata': flatten(row)}))
        found = {match[0] for match in delimiter_pattern(effective).finditer(text)} & result.keys()
        if found:
            fingerprint = hashlib.sha256(text.encode('utf-8')).hexdigest()
            for marker in found:
                result[marker][str(index)] = fingerprint
    return result


def edit(store, dataset, index, definitions, expected_revision, *, draft_text=None, expected_structure=None, expected_entry=None):
    """Atomically change the collection in the shared corpus history."""
    from corpus_overlay import working_view
    from metadata_editor import _commit
    with store.lock:
        data = store.load(dataset)
        if type(expected_revision) is not int or expected_revision != data.get('overlay_revision', 0):
            raise ValueError('The corpus changed. Refresh before editing delimiters.')
        if expected_structure is not None:
            if (not isinstance(expected_structure, dict) or expected_structure.get('index') != index or
                    not isinstance(expected_entry, tuple) or len(expected_entry) != 2):
                raise ValueError('Invalid project delimiter edit precondition.')
            current = store.record_structure(dataset, index)
            if current['structure_revision'] != expected_structure.get('revision'):
                raise ValueError('The entry list changed. Refresh before editing delimiters.')
        elif not 0 <= index < len(data['rows']):
            raise ValueError('Unknown record.')
        before = read(store, dataset)
        after = preserve_audio_revision(before, validate(definitions))
        if draft_text is not None and (not isinstance(draft_text, str) or len(draft_text) > 50000):
            raise ValueError('A source string must be text of at most 50,000 characters.')
        conflicts = source_conflicts(store, dataset, after,
                                     text_overrides={index: draft_text} if draft_text is not None else None)
        if conflicts:
            row, key, kind, value = conflicts[0]
            raise ValueError(f'Delimiter {key} conflicts with {kind} {value} in entry {row + 1}.')
        command = {'operations': [], 'mapping': data['mapping'],
                   'project_delimiters': {'before': before, 'after': after}}
        inverse = {'operations': [], 'mapping': data['mapping'],
                   'project_delimiters': {'before': after, 'after': before}}
        old_by_key = {item['key']: item for item in before}
        changed_markers = [item['representation'] for item in after
                           if item['key'] not in old_by_key or old_by_key[item['key']]['representation'] != item['representation']]
        inverse['safe_marker_uses'] = marker_use_fingerprints(store, dataset, changed_markers, after)
        if draft_text is not None:
            old_text = (store.record_structure(dataset, index) if expected_structure is not None else store.record(dataset, index))['text']
            if expected_structure is not None:
                validate_new_source_markers(store, dataset, old_text, draft_text, after)
            command['draft'] = {'index': index, 'before': old_text, 'after': draft_text}
            inverse['draft'] = {'index': index, 'before': draft_text, 'after': old_text}
        candidate = working_view(store.original_load(dataset), data['root'], data['mapping'], expected_revision + 1)
        history_id = _commit(store, dataset, expected_revision, candidate, command, inverse,
                             draft=command.get('draft'), project_delimiters=command['project_delimiters'],
                             expected_structure=expected_structure,allow_added_structure=expected_structure is not None,
                             expected_entry=expected_entry)
        return {'history_id': history_id, 'record': store.record_structure(dataset, index) if expected_structure is not None else store.record(dataset, index)}
