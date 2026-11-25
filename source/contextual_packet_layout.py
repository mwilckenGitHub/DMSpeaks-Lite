"""Compose independently returned context regions on one sample clock.

Regions have explicit ownership: each selected return supplies its own base
speech and its delimiter clip. Unclaimed original source cores appear once.
Locating these regions and reviewing speech content remain separate operations.
"""
import copy

from contextual_layer_layout import _validate_asset, _sha256_of_asset, _make_layer
from engine_limits import MAX_SEGMENTS


def interval(value, upper, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(label + ' needs two sample coordinates.')
    lo, hi = value
    if type(lo) is not int or type(hi) is not int or not 0 <= lo < hi <= upper:
        raise ValueError('Invalid ' + label + ' sample interval.')
    return lo, hi


def returned_bounds(context, returned_region, word_bounds):
    _, size = _validate_asset(context, 'returned context')
    begin, end = interval(returned_region, size, 'returned ownership')
    wa, wb = interval(word_bounds, size, 'delimiter')
    if not begin <= wa < wb <= end:
        raise ValueError('Keep the complete delimiter inside its returned region.')
    return begin, wa, wb, end


def packet_slices(ident, bounds):
    """Map stable clip IDs to the nonempty before, word and after slices."""
    prefix = 'packet:' + str(len(ident)) + ':' + ident
    return {prefix + ':' + role: (lo, hi)
            for role, lo, hi in zip(('before', 'word', 'after'), bounds, bounds[1:])
            if lo < hi}


def packet_for_layer(base, layer_id):
    """Find an occurrence by clip identity, independent of its track."""
    packets = (base.get('contextual_assembly') or {}).get('packets', ())
    return next((packet for packet in packets
                 if layer_id == f"packet:{len(packet['region_id'])}:{packet['region_id']}:word"), None)


def delimiter_track_id(kind):
    return 'delimiter:' + kind


def build_packet_layout(source, packets, *, context_links=None):
    source_id, count = _validate_asset(source, 'source')
    if not isinstance(packets, list) or len(packets) > MAX_SEGMENTS:
        raise ValueError('Supply a bounded list of returned context regions.')
    prepared, ids, orders = [], set(), set()
    for raw in packets:
        if not isinstance(raw, dict):
            raise ValueError('Invalid returned context region.')
        packet = copy.deepcopy(raw)
        ident = packet.get('id')
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError('Each returned region needs a unique nonempty identity.')
        ids.add(ident)
        context = packet.get('context_asset')
        context_id, _ = _validate_asset(context, 'returned context')
        lo, hi = interval(packet.get('source_region'), count, 'source ownership')
        begin, wa, wb, end = returned_bounds(context, packet.get('returned_region'), packet.get('word_bounds'))
        slot = packet.get('slot')
        if (not isinstance(slot, dict) or type(slot.get('order')) is not int or slot['order'] < 1
                or not isinstance(slot.get('type'), str) or slot['type'] in ('', 'fixed')):
            raise ValueError('Each returned region needs a replacement occurrence slot.')
        if slot['order'] in orders:
            raise ValueError('Replacement occurrence orders must be unique.')
        orders.add(slot['order'])
        prepared.append((lo, hi, ident, context_id, begin, end, wa, wb, slot, _sha256_of_asset(context)))
    prepared.sort(key=lambda packet: packet[0])
    if any(a[1] > b[0] for a, b in zip(prepared, prepared[1:])):
        raise ValueError('Returned regions cannot own overlapping original source samples.')
    tracks = [dict(id='base', name='Base speech', muted=False, gain_db=0)]
    layers, provenance, track_ids = [], [], set()

    def append(ident, track, asset, begin, end, order, slot, word_links=None):
        if begin == end:
            return
        layer = _make_layer(ident, track, asset, begin, end, order, slot=slot,
                            after=layers[-1]['id'] if layers else None,
                            anchor=None if layers else 0)
        if word_links:
            layer['context_word_links'] = copy.deepcopy(word_links)
        layers.append(layer)

    cursor = 0
    for i, (lo, hi, ident, context_id, begin, end, wa, wb, slot, digest) in enumerate(prepared):
        order = slot['order']
        prefix = 'packet:' + str(len(ident)) + ':' + ident
        before = dict(order=max(1, order - 1), type='fixed')
        after = dict(order=order + 1, type='fixed')
        append('source-core:' + str(i), 'base', source_id, cursor, lo, max(1, order - 1), before)
        kind = slot['type']
        track = delimiter_track_id(kind)
        if track not in track_ids:
            track_ids.add(track)
            tracks.append(dict(id=track, name=kind[0].upper() + kind[1:], muted=False, gain_db=0))
        word_links = (context_links or {}).get(ident)
        append(prefix + ':before', 'base', context_id, begin, wa, max(1, order - 1), before, word_links)
        append(prefix + ':word', track, context_id, wa, wb, order, slot)
        append(prefix + ':after', 'base', context_id, wb, end, order + 1, after, word_links)
        provenance.append(dict(region_id=ident, slot_order=order, source_region=[lo, hi],
                               returned_region=[begin, end], word_bounds=[wa, wb], context_sha256=digest))
        cursor = hi
    final_order = prepared[-1][8]['order'] + 1 if prepared else 1
    append('source-core:final', 'base', source_id, cursor, count, final_order, dict(order=final_order, type='fixed'))
    if len(layers) > MAX_SEGMENTS:
        raise ValueError('These returned regions exceed the runtime clip limit.')
    return dict(tracks=tracks, layers=layers, provenance=dict(format='dmspeaks-context-packets-1',
        source_sha256=_sha256_of_asset(source), packets=provenance, word_ownership_review_required=True,
        scope='explicit nonoverlapping source/context regions; no automatic acoustic ownership claim'))
