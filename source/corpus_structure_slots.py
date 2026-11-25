"""Stable corpus display slots; structural editing is not enabled yet."""

MAX_SLOTS = 1_000_000


def _number(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('Invalid slot or position')
    return value


def validate(state):
    if not isinstance(state, dict) or set(state) != {'version', 'next_slot', 'order', 'hidden'}:
        raise ValueError('Invalid slot state')
    if type(state['version']) is not int or state['version'] != 1:
        raise ValueError('Unsupported slot version')
    count = _number(state['next_slot'], MAX_SLOTS)
    if not isinstance(state['order'], list) or not isinstance(state['hidden'], list):
        raise ValueError('Slot lists required')
    slots = state['order'] + state['hidden']
    if len(slots) != count or any(type(slot) is not int or slot < 0 or slot >= count for slot in slots):
        raise ValueError('Invalid slot membership')
    if len(set(slots)) != count:
        raise ValueError('Duplicate slot')
    return {'version': 1, 'next_slot': count, 'order': list(state['order']), 'hidden': list(state['hidden'])}


def initial(count):
    count = _number(count, MAX_SLOTS)
    return {'version': 1, 'next_slot': count, 'order': list(range(count)), 'hidden': []}


def add(state, position):
    result = validate(state)
    _number(position, len(result['order']))
    if result['next_slot'] >= MAX_SLOTS:
        raise ValueError('Slot limit reached')
    slot = result['next_slot']
    result['next_slot'] += 1
    result['order'].insert(position, slot)
    return result, slot


def hide(state, slot):
    result = validate(state)
    _number(slot, result['next_slot'])
    if slot not in result['order']:
        raise ValueError('Slot is not visible')
    result['order'].remove(slot)
    result['hidden'].append(slot)
    return result


def restore(state, slot, position):
    result = validate(state)
    _number(slot, result['next_slot'])
    _number(position, len(result['order']))
    if slot not in result['hidden']:
        raise ValueError('Slot is not hidden')
    result['hidden'].remove(slot)
    result['order'].insert(position, slot)
    return result


def move(state, slot, position):
    result = validate(state)
    _number(slot, result['next_slot'])
    if slot not in result['order']:
        raise ValueError('Slot is not visible')
    _number(position, len(result['order']) - 1)
    result['order'].remove(slot)
    result['order'].insert(position, slot)
    return result


def restore_snapshot(current, snapshot):
    """Restore order/visibility while retaining the current allocation watermark.

    The caller must ensure snapshots belong to the same project, serialize
    changes transactionally, and clear redo history when branching.
    """
    current = validate(current)
    result = validate(snapshot)
    if result['next_slot'] > current['next_slot']:
        raise ValueError('Snapshot contains unallocated slots')
    result['hidden'].extend(range(result['next_slot'], current['next_slot']))
    result['next_slot'] = current['next_slot']
    return validate(result)
