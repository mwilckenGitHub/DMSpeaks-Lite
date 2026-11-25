"""Corpus-owned lookup rules and read-only discovery of source markup."""
import copy
import json
import re
from collections import Counter

from json_fields import apply_field, escape, pointer_parts
from stage_directions import XML_TAG, classify_emote

EXTENSION = '_dmspeaks_corpus_lookup'
FIELD = EXTENSION + '_json'
ACTIVE = EXTENSION + '_markers'
FORMAT = 'dmspeaks-corpus-lookup'
TYPES = {
    'metadata': 'Metadata', 'speaker': 'Speaker', 'language': 'Language',
    'record_id': 'Record ID', 'editorial_note': 'Editorial Note',
    'pronunciation_note': 'Pronunciation Note', 'audio_reference': 'Audio Reference',
    'timecode': 'Timecode', 'formatting': 'Formatting', 'omit_marker': 'Omit Marker',
    'line_break': 'Line Break', 'paragraph_break': 'Paragraph Break', 'emote': 'Emote',
    'speech_direction': 'Speech Direction', 'sound_effect': 'Sound Effect Cue',
}
MARKER_TYPES = ('formatting', 'omit_marker', 'line_break', 'paragraph_break',
                'emote', 'speech_direction', 'sound_effect')
FIELD_TYPES = tuple(kind for kind in TYPES
                    if kind not in MARKER_TYPES or kind in ('formatting', 'line_break', 'emote', 'speech_direction'))
CUE_TYPES = ('emote', 'speech_direction', 'sound_effect')
MARKER_OUTPUTS = {'omit_marker': ' ', 'line_break': '\n', 'paragraph_break': '\n\n'}
MAX_RULES = 256
MAX_DISCOVERED = 4000
DISPLAY_NAME = re.compile(r'^/?(?:b|bold|strong|i|italic|u|underline|strike)$', re.I)
TAG = re.compile(r'\[\[[^\[\]\r\n]{1,236}\]\]|\[[^\[\]\r\n]{1,238}\]|<[^<>\r\n]{1,238}>|%[\w.-]{1,238}%')
BREAK = re.compile(r'\$[bB]')
CUE = re.compile(r"[\w .,'’-]{1,160}\Z", re.UNICODE)


class LookupRuleError(ValueError):
    """Validation error tied to one editable field in the submitted rules."""

    def __init__(self, message, index, field):
        super().__init__(message)
        self.rule = {'index': index, 'field': field}


def marker_problem(marker):
    from model import TOKEN
    from delimiter_definitions import DISPLAY_BRACKET

    if TOKEN.search(marker) or BREAK.search(marker):
        return 'Built-in substitutions and line breaks already have source rules.'
    if not TAG.fullmatch(marker):
        return 'Enter a complete tag such as <bold>, [[wave]] or %SCENE%.'
    if marker.startswith('<') and XML_TAG.fullmatch(marker[1:-1].strip()):
        return 'This tag is reserved for SSML.'
    if marker.startswith('[') and not marker.startswith('[[') and not DISPLAY_BRACKET.fullmatch(marker):
        return 'Single square brackets are reserved for speech controls. Use a source tag such as [[wave]].'
    return ''


def validate(config):
    if not isinstance(config, dict) or config.get('format') != FORMAT or type(config.get('version')) is not int or config['version'] != 1:
        raise ValueError('Invalid corpus lookup table format or version.')
    rules = config.get('rules')
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise ValueError(f'A corpus can have up to {MAX_RULES} lookup rules.')
    normalized, seen = [], set()
    for number, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            raise ValueError(f'Rule {number} must be an object.')
        source, match, kind = (rule.get(key) for key in ('source', 'match', 'type'))
        cue = rule.get('cue', '')
        if source not in ('marker', 'field') or not isinstance(match, str) or not 1 <= len(match) <= 240 or not match.isprintable():
            raise ValueError(f'Rule {number} needs a field path or source marker of up to 240 characters.')
        if kind not in (MARKER_TYPES if source == 'marker' else FIELD_TYPES):
            raise LookupRuleError(f'Choose a valid type for {match}.', number - 1, 'type')
        if source == 'field' and not pointer_parts(match):
            raise ValueError(f'Rule {number} needs a field path such as /questTitle.')
        if source == 'marker':
            problem = marker_problem(match)
            if problem:
                raise ValueError(f'{match} · {problem}')
            if match.startswith('[') and not match.startswith('[[') and kind != 'formatting':
                raise LookupRuleError(f'{match} can only be used as a formatting rule.', number - 1, 'type')
        if (source, match) in seen:
            raise ValueError(f'{match} already has a lookup rule.')
        seen.add((source, match))
        if not isinstance(cue, str):
            raise LookupRuleError(f'Enter a short cue in plain text for {match}.', number - 1, 'cue')
        cue = cue.strip()
        if cue and not CUE.fullmatch(cue):
            raise LookupRuleError(f'Use a short cue without markup for {match}.', number - 1, 'cue')
        if source == 'marker' and kind in ('speech_direction', 'sound_effect') and not cue:
            raise LookupRuleError(f'Enter a {TYPES[kind].lower()} for {match}.', number - 1, 'cue')
        normalized.append({'source': source, 'match': match, 'type': kind,
                           'cue': cue if source == 'marker' and kind in CUE_TYPES else ''})
    return {'format': FORMAT, 'version': 1, 'rules': normalized}


def location(data):
    if data['container'] is not None:
        return data['root'], EXTENSION, '/' + EXTENSION
    key = FIELD if data['kind'] == 'csv' else EXTENSION
    return data['rows'][0], key, '/0/' + key


def read(data):
    owner, key, _ = location(data)
    if key not in owner or data['kind'] == 'csv' and owner[key] in (None, ''):
        return {'format': FORMAT, 'version': 1, 'rules': []}
    value = owner[key]
    if data['kind'] == 'csv':
        if not isinstance(value, str):
            raise ValueError('Invalid CSV lookup table.')
        value = json.loads(value)
    # Keep older trial files readable without activating literal speech rules.
    if isinstance(value, dict) and isinstance(value.get('rules'), list) and len(value['rules']) <= MAX_RULES:
        value = {**value, 'rules': [rule for rule in value['rules']
                                   if not isinstance(rule, dict) or rule.get('type') != 'spoken_replacement']}
    return validate(value)


def field_types(data):
    return {rule['match']: TYPES[rule['type']] for rule in data['lookup']['rules'] if rule['source'] == 'field'}


def marker_rules(config, text):
    return [rule for rule in config['rules'] if rule['source'] == 'marker' and rule['match'] in text]


def source_controls(record, delimiters):
    """Find exact marker spans that do not overlap a dialogue-owned delimiter."""
    from model import delimiter_pattern

    rules = {rule['match']: rule for rule in record.get('all_metadata', {}).get(ACTIVE, [])}
    if not rules:
        return []
    protected = iter(delimiter_pattern(delimiters).finditer(record['text']))
    prior = next(protected, None)
    controls = []
    for match in TAG.finditer(record['text']):
        rule = rules.get(match[0])
        if rule is None:
            continue
        start, end = match.span()
        while prior is not None and prior.end() <= start:
            prior = next(protected, None)
        if prior is None or prior.start() >= end:
            controls.append((start, end, rule))
    return controls


def cue_events(text, controls):
    events = []
    for start, end, rule in controls:
        kind, cue = rule['type'], rule['cue']
        if kind not in CUE_TYPES:
            continue
        if kind == 'speech_direction':
            role = 'intonation'
        elif kind == 'sound_effect':
            role = 'sound_effect'
        else:
            role = 'vocal' if cue else 'visual'
        events.append({'event_id': f'corpus-lookup-{start}', 'kind': role,
                       'source_start': start, 'source_end': end, 'original_markup': text[start:end],
                       'tts_markup': f'[{cue}]' if cue else '', 'lookup_rule': True,
                       'speaker_role': 'npc' if role in ('intonation', 'vocal') else None,
                       'spoken': False, 'review_required': False,
                       'narration': {'eligible': False, 'text': ''}})
    return events


def stage_root(data, config):
    """Return the edited corpus root and the reversible field operations."""
    owner, key, pointer = location(data)
    value = json.dumps(config, ensure_ascii=False) if data['kind'] == 'csv' else config
    command = {'operation': 'replace' if key in owner else 'add', 'pointer': pointer, 'value': value}
    root = copy.deepcopy(data['root'])
    inverse = apply_field(root, command)
    return root, command, inverse


def write(store, dataset, rules, expected_revision, *, profile=None, expected_structure=None, expected_entry=None):
    from corpus_overlay import working_view
    from metadata_editor import _commit

    config = validate({'format': FORMAT, 'version': 1, 'rules': rules})
    check_provider(config['rules'], profile)
    with store.lock:
        data = store.load(dataset)
        revision = data.get('overlay_revision', 0)
        if type(expected_revision) is not int or revision != expected_revision:
            raise ValueError('The corpus changed. Reopen the lookup table before applying rules.')
        if config == data['lookup']:
            return {'revision':revision,'history_id':None} if expected_structure is not None else revision
        root, command, inverse = stage_root(data, config)
        mapping = data['mapping']
        candidate = working_view(store.original_load(dataset), root, mapping, revision + 1)
        history_id = _commit(store, dataset, revision, candidate, {'operations': [command], 'mapping': mapping},
                {'operations': [inverse], 'mapping': mapping}, expected_structure=expected_structure,
                expected_entry=expected_entry, allow_lookup_structure=expected_structure is not None)
        return {'revision':revision + 1,'history_id':history_id} if expected_structure is not None else revision + 1


def check_provider(rules, profile):
    from delimiter_definitions import check_provider_marker

    for rule in rules:
        marker = rule['match']
        if rule['source'] == 'marker' and marker.startswith('[') and not marker.startswith('[['):
            check_provider_marker({'representation': marker, 'type': rule['type']}, profile)


def check_sound_effects(events, profile):
    if (any(event['kind'] == 'sound_effect' for event in events)
            and (profile.get('syntax') != 'brackets' or 'directions' not in profile.get('capabilities', '').split(','))):
        raise ValueError('Sound Effect Cues require a connection with inline bracket directions.')


def _fields(row, prefix=''):
    for key, value in row.items():
        if key.startswith(('_dmspeaks_', '_voxweave_')):
            continue
        pointer = prefix + '/' + escape(key)
        yield pointer, value
        if isinstance(value, dict):
            yield from _fields(value, pointer)


def _field_suggestion(path):
    name = path.rsplit('/', 1)[-1].lower()
    if 'emote' in name or 'performance_event' in name:
        return 'emote'
    if any(word in name for word in ('direction', 'intonation', 'prosody')):
        return 'speech_direction'
    if any(word in name for word in ('formatting', 'text_style', 'display_style')):
        return 'formatting'
    return {
        'speaker': 'speaker', 'speaker_ref': 'speaker', 'locale': 'language', 'language': 'language',
        'id': 'record_id', 'record_id': 'record_id', 'editorial_note': 'editorial_note',
        'pronunciation_note': 'pronunciation_note', 'audio_reference': 'audio_reference',
        'timecode': 'timecode',
    }.get(name, 'metadata')


def _marker_suggestion(marker):
    if DISPLAY_NAME.fullmatch(marker.strip('<>[]')):
        return 'formatting', ''
    conventional = {
        'line_break': 'line_break', 'paragraph_break': 'paragraph_break',
        'edit_point': 'omit_marker', 'production_note': 'omit_marker',
    }.get(marker.strip('[]').casefold()) if marker.startswith('[[') else None
    if conventional:
        return conventional, ''
    if marker.startswith('<'):
        category, cue = classify_emote(marker[1:-1])
        if category in ('visual', 'vocal', 'intonation'):
            return ('speech_direction' if category == 'intonation' else 'emote'), cue.strip('[]')
        if marker[1:-1].strip().lower() == 'emote':
            return 'emote', ''
    return '', ''


def discover(store, dataset, *, structured=False):
    from model import TOKEN, getfield, flatten
    from delimiter_definitions import registered
    from json_fields import mapped_pointer

    with store.lock:
        own_transaction = not store.db.in_transaction
        if own_transaction: store.db.execute('BEGIN')
        try:
            data = store.load(dataset)
            drafts = store.draft_map(dataset)
            from project_delimiter_library import read as read_project_delimiters
            project_definitions = read_project_delimiters(store, dataset)
            if structured:
                from corpus_structure_commands import read_state
                from project_delimiter_library import _entries
                structure = read_state(store, dataset)
                visible = set(structure['snapshot']['state']['order'])
                rows = [(index, row, text) for index, row, text in _entries(store, dataset) if index in visible]
            else:
                structure = None
                rows = [(index, row, None) for index, row in enumerate(data['rows'])]
            if own_transaction: store.db.commit()
        except BaseException:
            if own_transaction: store.db.rollback()
            raise
    mapping = data['mapping']
    counts, observed, reserved = Counter(), {}, set()
    truncated = False

    def count(source, value, amount=1):
        nonlocal truncated
        key = source, value
        if key not in counts and len(counts) >= MAX_DISCOVERED:
            truncated = True
            return False
        counts[key] += amount
        return True

    for index, row, captured_text in rows:
        text_paths = {'/' + escape(key) for key in data['slots']}
        if mapping['text'] != '@segments':
            text_paths.add(mapped_pointer(row, mapping['text']))
        if mapping['text'] == 'originalText' and isinstance(data['root'], dict) and 'schemaVersion' in data['root']:
            text_paths.add('/text')
        for path, value in _fields(row):
            if path in text_paths or len(path) > 240:
                continue
            if count('field', path):
                observed.setdefault(path, set()).add(type(value).__name__)
        if captured_text is not None:
            text = captured_text
        elif index in drafts:
            text = drafts[index]
        elif mapping['text'] == '@segments':
            text = ''.join(str(row.get(key) or '') for key in data['slots'])
        else:
            text = str(getfield(row, mapping['text'], '') or '')
        for pattern in (TAG, TOKEN, BREAK):
            for match in pattern.finditer(text):
                if len(match[0]) <= 240:
                    count('marker', match[0])
        if project_definitions or '_dmspeaks_delimiters' in row or '_dmspeaks_delimiters_json' in row:
            for definition in registered({'all_metadata': flatten(row), 'project_delimiters': project_definitions}):
                marker = definition['representation']
                reserved.add(marker)
                if not any(pattern.fullmatch(marker) for pattern in (TAG, TOKEN, BREAK)) and marker in text:
                    count('marker', marker, text.count(marker))

    kind_labels = {'str': 'Text', 'int': 'Number', 'float': 'Number', 'bool': 'True/False',
                   'NoneType': 'Empty', 'dict': 'Object', 'list': 'List'}
    items = []
    for (source, value), hits in sorted(counts.items(), key=lambda item: (item[0][0], -item[1], item[0][1])):
        if source == 'field':
            suggestion, cue, problem = _field_suggestion(value), '', ''
        else:
            suggestion, cue = _marker_suggestion(value)
            problem = marker_problem(value)
        items.append({'source': source, 'match': value, 'count': hits, 'suggestion': suggestion, 'cue': cue,
                      'observed': sorted({kind_labels.get(kind, kind) for kind in observed.get(value, ())}),
                      'reserved': problem, 'entry_override': source == 'marker' and value in reserved})
    result = {'revision': data.get('overlay_revision', 0), 'rules': data['lookup']['rules'], 'items': items,
            'entry_count': len(rows), 'truncated': truncated, 'types': TYPES, 'marker_types': MARKER_TYPES,
            'field_types': FIELD_TYPES, 'limit': MAX_RULES}
    if structure is not None: result['structure_revision'] = structure['revision']
    return result


def preview(store, dataset, index, rules, player, *, profile=None, record=None):
    from performance_plan import parse_record

    config = validate({'format': FORMAT, 'version': 1, 'rules': rules})
    check_provider(config['rules'], profile)
    record = store.record(dataset, index) if record is None else record
    record['all_metadata'][ACTIVE] = marker_rules(config, record['text'])
    parsed = parse_record(record, player)
    result = {key: parsed[key] for key in ('resolved_text', 'spoken_text', 'missing_values')}
    result['unmapped_markers'] = list(dict.fromkeys(
        match[0] for match in TAG.finditer(parsed['resolved_text']) if not marker_problem(match[0])))
    if profile is not None:
        try:
            check_sound_effects(parsed.get('performance_events', []), profile)
        except ValueError as error:
            result['provider_warning'] = str(error)
    return result
