"""Transactional entry-list commands for the future Corpus workspace.

This service is not exposed to the workspace yet. Slots identify entries in the
original import, or permanently allocated new entries; they are never recycled.
The portable structure and its local undo/redo journal update in one SQLite
transaction. Original corpus bytes and existing asset ownership never change.
"""
import json
import time

import corpus_structure_slots as slots
from corpus_structure_snapshot import read, setting_key, source_binding, validate


class StructureConflict(ValueError):
    """An entry or its containing corpus changed after the caller read it."""


def _source(store, dataset):
    data = store.original_load(dataset)
    original = (store.folder / (dataset + '.original')).read_bytes()
    source_id, count = source_binding(original, data['mapping'], len(data['rows']))
    if source_id[:20] != dataset:
        raise ValueError('The original corpus no longer matches this project.')
    return original, data['mapping'], source_id, count


def _initial(source_id, count):
    return {'version': 1, 'source_id': source_id, 'original_count': count,
            'state': slots.initial(count), 'added_entries': {}}


def _journal(store, dataset):
    row = store.db.execute('SELECT revision,undo,redo FROM corpus_structure_history WHERE dataset=?', (dataset,)).fetchone()
    if row is None:
        return 0, [], []
    return row[0], json.loads(row[1]), json.loads(row[2])


def read_state(store, dataset):
    """Return structure and journal from one consistent database snapshot."""
    with store.lock:
        db = store.db
        own_transaction = not db.in_transaction
        if own_transaction:
            db.execute('BEGIN')
        try:
            original, mapping, source_id, count = _source(store, dataset)
            value = read(store, dataset)
            snapshot = value if value is not None else _initial(source_id, count)
            snapshot = validate(snapshot, original, mapping, count)
            revision, undo, redo = _journal(store, dataset)
            result = {'snapshot': snapshot, 'revision': revision,
                      'can_undo': bool(undo), 'can_redo': bool(redo)}
            if own_transaction:
                db.commit()
            return result
        except BaseException:
            if own_transaction:
                db.rollback()
            raise


def public_state(state):
    """Expose the order and command availability without the internal journal."""
    return {'revision': state['revision'], 'can_undo': state['can_undo'],
            'can_redo': state['can_redo'], 'order': state['snapshot']['state']['order']}


def require_visible(store, dataset, expected, *, original_only=False):
    """Check a stable-slot precondition inside the caller's write transaction."""
    if (not isinstance(expected, dict) or set(expected) != {'revision', 'index'} or
            type(expected['revision']) is not int or type(expected['index']) is not int):
        raise ValueError('Invalid entry precondition.')
    state = read_state(store, dataset)
    slot = expected['index']
    if (state['revision'] != expected['revision'] or slot < 0 or
            (original_only and slot >= state['snapshot']['original_count']) or
            slot not in state['snapshot']['state']['order']):
        raise StructureConflict('The entry list changed. Refresh before saving.')


def require_visible_original(store, dataset, expected):
    require_visible(store, dataset, expected, original_only=True)


def _execute(snapshot, command):
    """Apply one validated command and return its inverse and allocated slot."""
    if not isinstance(command, dict) or not isinstance(command.get('action'), str):
        raise ValueError('Invalid entry command.')
    state = snapshot['state']
    action = command['action']
    allocated = None
    if action == 'add' and set(command) == {'action', 'position', 'content'}:
        state, allocated = slots.add(state, command['position'])
        snapshot['added_entries'][str(allocated)] = command['content']
        inverse = {'action': 'hide', 'slot': allocated}
    elif action == 'hide' and set(command) == {'action', 'slot'}:
        slot = command['slot']
        if type(slot) is not int or slot not in state['order']:
            raise ValueError('Entry is not visible.')
        position = state['order'].index(slot)
        state = slots.hide(state, slot)
        inverse = {'action': 'restore', 'slot': slot, 'position': position}
    elif action == 'restore' and set(command) == {'action', 'slot', 'position'}:
        slot = command['slot']
        state = slots.restore(state, slot, command['position'])
        inverse = {'action': 'hide', 'slot': slot}
    elif action == 'move' and set(command) == {'action', 'slot', 'position'}:
        slot = command['slot']
        if type(slot) is not int or slot not in state['order']:
            raise ValueError('Entry is not visible.')
        position = state['order'].index(slot)
        state = slots.move(state, slot, command['position'])
        inverse = {'action': 'move', 'slot': slot, 'position': position}
    elif action == 'edit' and set(command) in ({'action', 'slot', 'content'}, {'action', 'slot', 'content', 'draft_restore', 'expected_draft'}):
        slot = command['slot']
        if type(slot) is not int or slot not in state['order'] or slot < snapshot['original_count']:
            raise ValueError('Choose a visible added entry.')
        previous = snapshot['added_entries'][str(slot)]
        snapshot['added_entries'][str(slot)] = command['content']
        inverse = {'action': 'edit', 'slot': slot, 'content': previous}
    else:
        raise ValueError('Invalid entry command.')
    snapshot['state'] = state
    return inverse, allocated


def apply(store, dataset, expected_revision, command, *, expected_entry=None):
    """Apply an edit, undo or redo atomically, rejecting stale windows."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('Invalid entry revision.')
    with store.lock:
        db = store.db
        if db.in_transaction:
            raise ValueError('Finish the current project change first.')
        db.execute('BEGIN IMMEDIATE')
        try:
            original, mapping, source_id, count = _source(store, dataset)
            snapshot = read(store, dataset) or _initial(source_id, count)
            revision, undo, redo = _journal(store, dataset)
            if revision != expected_revision:
                raise StructureConflict('Entry list changed in another window.')
            snapshot = validate(snapshot, original, mapping, count)
            action = command.get('action') if isinstance(command, dict) else None
            if expected_entry is not None:
                if (not isinstance(expected_entry, dict) or set(expected_entry) != {'source_revision', 'metadata_revision'}
                        or action != 'edit' or type(command.get('slot')) is not int):
                    raise ValueError('Invalid entry edit precondition.')
                current = store.record_structure(dataset, command['slot'])
                if (current['revision'] != expected_entry['source_revision'] or
                        current['metadata_revision'] != expected_entry['metadata_revision']):
                    raise StructureConflict('This entry changed elsewhere. Copy your unsaved edits before reopening it.')
            if action in ('undo', 'redo') and set(command) == {'action'}:
                source, target = (undo, redo) if action == 'undo' else (redo, undo)
                if not source:
                    raise ValueError('No entry history available.')
                effective = source.pop()
            else:
                effective = command
            if isinstance(effective, dict) and effective.get('action') == 'edit':
                slot = effective.get('slot')
                content = effective.get('content')
                if (type(slot) is not int or str(slot) not in snapshot['added_entries'] or
                        not isinstance(content, dict) or not isinstance(content.get('text'), str)):
                    raise ValueError('Invalid entry command.')
                draft=store.draft_map(dataset).get(slot)
                if action == 'edit' and ('draft_restore' in effective or 'expected_draft' in effective):
                    raise ValueError('Invalid entry command.')
                if action == 'edit' and draft is not None:
                    effective={**effective,'draft_restore':None,'expected_draft':draft}
                if 'draft_restore' in effective:
                    target_draft=effective['draft_restore']
                    if target_draft is not None and (not isinstance(target_draft,str) or len(target_draft)>50000):
                        raise ValueError('Invalid entry draft history.')
                    if draft != effective.get('expected_draft'):
                        raise StructureConflict('This entry draft changed elsewhere. Refresh before undoing or saving.')
                elif draft is not None and action in ('undo','redo'):
                    raise StructureConflict('This entry has a newer draft. Undo the draft before changing its entry history.')
                from source_text_lock import check_text
                current_text=draft if draft is not None else snapshot['added_entries'][str(slot)]['text']
                target_text=effective.get('draft_restore') if effective.get('draft_restore') is not None else content['text']
                check_text(store, dataset, current_text, target_text)
                # Autosave may submit a form whose values have not changed.
                # Do not add a misleading Undo step or discard a Redo branch.
                if (action == 'edit' and 'draft_restore' not in effective and
                        json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False) ==
                        json.dumps(snapshot['added_entries'][str(slot)], sort_keys=True, ensure_ascii=False, allow_nan=False)):
                    db.commit()
                    return {'snapshot': snapshot, 'revision': revision, 'allocated': None,
                            'can_undo': bool(undo), 'can_redo': bool(redo)}
            inverse, allocated = _execute(snapshot, effective)
            if effective.get('action') == 'edit' and 'draft_restore' in effective:
                slot=effective['slot'];target_draft=effective['draft_restore']
                inverse.update(draft_restore=draft,expected_draft=target_draft)
                if target_draft is None:db.execute('delete from drafts where dataset=? and record=?',(dataset,slot))
                else:db.execute('insert or replace into drafts values(?,?,?,?)',(dataset,slot,target_draft,time.time()))
            if action in ('undo', 'redo'):
                target.append(inverse)
            else:
                undo.append(inverse)
                redo.clear()
            snapshot = validate(snapshot, original, mapping, count)
            revision += 1
            db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                       (setting_key(dataset), json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))
            db.execute('INSERT OR REPLACE INTO corpus_structure_history(dataset,revision,undo,redo) VALUES(?,?,?,?)',
                       (dataset, revision, json.dumps(undo, ensure_ascii=False, allow_nan=False),
                        json.dumps(redo, ensure_ascii=False, allow_nan=False)))
            db.commit()
            return {'snapshot': snapshot, 'revision': revision, 'allocated': allocated,
                    'can_undo': bool(undo), 'can_redo': bool(redo)}
        except BaseException:
            db.rollback()
            raise


def save_added_entry(store, dataset, slot, text, metadata, *, expected_structure_revision,
                     expected_source_revision, expected_metadata_revision):
    """Save an added entry with optimistic source and metadata checks.

    The visible workspace does not call this service until structural editing,
    history, recovery and export are integrated end to end.
    """
    return apply(store, dataset, expected_structure_revision,
                 {'action': 'edit', 'slot': slot, 'content': {'text': text, 'metadata': metadata}},
                 expected_entry={'source_revision': expected_source_revision,
                                 'metadata_revision': expected_metadata_revision})
