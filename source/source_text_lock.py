"""Project-owned protection for corpus dialogue, shared by both editors.

The lock prevents editing; it is not an access-control or encryption boundary.
Metadata, delimiter choices and audio remain editable while text is locked.
"""


SETTING_PREFIX = 'corpus_source_lock:'
LOCK_MESSAGE = 'Source text is locked. Unlock Source Text before changing dialogue.'


def is_locked(store, dataset):
    return store.setting(SETTING_PREFIX + dataset, False) is True


def set_locked(store, dataset, locked, *, expected=None):
    if type(locked) is not bool or expected is not None and type(expected) is not bool:
        raise ValueError('Source text lock must be true or false.')
    with store.lock:
        store.load(dataset)
        if expected is not None and is_locked(store, dataset) != expected:
            raise ValueError('Source text protection changed elsewhere. Reopen the dialogue before changing it.')
        store.set_setting(SETTING_PREFIX + dataset, locked)
        return {'dataset': dataset, 'source_text_locked': locked}


def check_text(store, dataset, before, after):
    if before != after and is_locked(store, dataset):
        raise ValueError(LOCK_MESSAGE)


def _source_texts(data):
    from model import getfield
    field = data['mapping']['text']
    for row in data['rows']:
        if field == '@segments':
            yield ''.join(str(row.get(key) or '') for key in data['slots'])
        else:
            yield str(getfield(row, field, '') or '')


def check_overlay(store, dataset, candidate, draft=None, *, structured=False):
    """Reject the whole metadata/history transaction if it changes dialogue."""
    if not is_locked(store, dataset):
        return
    previous = store.load(dataset)
    if list(_source_texts(previous)) != list(_source_texts(candidate)):
        raise ValueError(LOCK_MESSAGE)
    if draft is not None:
        current = (store.record_structure(dataset, draft['index']) if structured
                   else store.record(dataset, draft['index']))
        if current['text'] != draft['after']:
            raise ValueError(LOCK_MESSAGE)
