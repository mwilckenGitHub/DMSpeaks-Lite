"""Stable application identity without adding fields to imported corpus data."""
from uuid import UUID, uuid5


ENTRY_NAMESPACE = UUID('e7ec7d74-3a58-5bc5-a774-d4ef109cb442')


def identity(dataset, index, source_id):
    """Use the immutable import and row position, never editable dialogue.

    Current corpus overlays preserve row order. Any future structural editor
    must carry these identities when moving rows rather than recompute them.
    Portable projects preserve the original import bytes and mapping, so they
    retain the same dataset identity on another computer.
    """
    return {
        'source_id': None if source_id is None or source_id == '' else source_id,
        'internal_id': 'dms-' + str(uuid5(ENTRY_NAMESPACE, f'{dataset}:{index}')),
    }
