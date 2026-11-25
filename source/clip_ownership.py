"""Keep recordings associated with the imported dialogue they belong to.

Current corpus overlays retain import row order. Structural editing must carry
entry identities forward explicitly before it may reuse these recordings.
"""
from collections import Counter

from authoring_catalog import canonical_key


def source_id_counts(data, getfield):
    counts = data.get('_clip_source_id_counts')
    if counts is None:
        mapping = data['mapping']
        ids = []
        for index, row in enumerate(data['rows']):
            value = getfield(row, mapping.get('id'))
            ids.append(value if value not in (None, '') else f'row-{index + 1}')
        counts = Counter(canonical_key(value) for value in ids)
        data['_clip_source_id_counts'] = counts
    return counts


def record_id(record):
    return f"{record['dataset']}:{record['index']}"


def current_record(store, dataset, index):
    """Resolve an asset owner through stable slots when a structure exists.

    Hidden entries deliberately fail rather than being reassigned to a visible
    row that happens to occupy the same position in the list.
    """
    from corpus_structure_snapshot import read as read_structure
    if read_structure(store, dataset) is not None:
        return store.record_structure(dataset, index)
    return store.record(dataset, index)


def belongs_to_record(asset, record):
    owner = asset.get('record_id')
    if owner in (None, ''):
        return not record.get('source_id_ambiguous', False)
    return owner == record_id(record)
