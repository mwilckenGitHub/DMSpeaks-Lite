"""Authored relationships between fresh clip guides on the shared sample clock.

A relationship moves complete processed clips. It neither edits their samples
nor turns estimated word/activity times into verified phoneme boundaries.
"""
import copy
from engine_limits import MAX_AUDIO_SECONDS

RATE = 44100
GUIDE_IDS = frozenset({'clip_start', 'clip_end', 'provider_start', 'provider_end',
                       'returned_generation_start', 'returned_generation_end',
                       'returned_alignment_start', 'returned_alignment_end', 'leader', 'decay'})
FIELDS = frozenset({'target_layer_id', 'target_guide_id', 'own_guide_id', 'offset_samples'})


def binding(layer):
    value = layer.get('guide_binding')
    if value is None:return None
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('A snap link needs its target clip, both guides and a signed sample offset.')
    target = value['target_layer_id']
    if not isinstance(target, str) or not target or len(target) > 512:
        raise ValueError('Choose an existing target clip for the snap link.')
    if any(not isinstance(value[key], str) or value[key] not in GUIDE_IDS for key in ('target_guide_id', 'own_guide_id')):
        raise ValueError('Choose existing clip guides for the snap link.')
    offset = value['offset_samples']
    if type(offset) is not int or abs(offset) > RATE*MAX_AUDIO_SECONDS:
        raise ValueError('The snap offset must be a signed sample count within the preview capacity.')
    if layer.get('after_layer_id') is not None or layer.get('gap_samples', 0) or layer.get('overlap_samples', 0):
        raise ValueError('A snap link replaces the preceding-clip gap and overlap settings.')
    return value


def parent_id(layer):
    link = binding(layer)
    if link:return link['target_layer_id']
    parent = layer.get('after_layer_id')
    if parent is not None and (not isinstance(parent, str) or not parent):
        raise ValueError('Invalid preceding layer ID.')
    return parent


def validate_graph(layers):
    """Validate references before portable writes or graph edits, without audio I/O."""
    if not isinstance(layers, list):raise ValueError('Use an array of clips.')
    ids = {}
    for layer in layers:
        ident = layer.get('id') if isinstance(layer, dict) else None
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError('Clip identifiers must be unique nonempty strings.')
        ids[ident] = layer
    done, visiting = set(), set()
    def visit(ident):
        if ident in done:return
        if ident in visiting:raise ValueError('The timing dependencies contain a cycle.')
        if ident not in ids:raise ValueError('A timing dependency refers to an unknown clip.')
        visiting.add(ident)
        parent = parent_id(ids[ident])
        if parent is not None:visit(parent)
        visiting.remove(ident);done.add(ident)
    for ident in ids:visit(ident)


def guide(layer, ident):
    count = layer.get('rendered_sample_count')
    if type(count) is not int or count < 0:
        raise ValueError('Measure the complete clip before resolving its snap link.')
    if ident in ('clip_start', 'clip_end'):
        return {'id':ident, 'kind':'clip', 'sample':0 if ident == 'clip_start' else count}
    packet = layer.get('snap_points')
    if (not isinstance(packet, dict) or packet.get('format') != 'dmspeaks-clip-snap-points' or
            packet.get('sample_rate') != RATE or packet.get('sample_count') != count or
            not isinstance(packet.get('points'), list)):
        raise ValueError('The snap link needs fresh measured guides. Rebuild or detach it.')
    points = [p for p in packet['points'] if isinstance(p, dict) and p.get('id') == ident]
    if len(points) != 1:
        minimum = layer.get(ident + '_min_dbfs') if ident in ('leader', 'decay') else None
        if not points and type(minimum) in (int, float):
            edge = 'Leader' if ident == 'leader' else 'Cutoff'
            name = layer.get('label') or layer.get('slot', {}).get('spoken_text') or layer['id']
            raise ValueError(f'The linked {edge} guide for “{name}” is unavailable at {minimum:g} dBFS. '
                             'Lower that minimum level or choose another snap guide.')
        raise ValueError('A linked guide is unavailable. Choose another guide or detach the snap link.')
    point = points[0]
    if type(point.get('sample')) is not int or not 0 <= point['sample'] <= count or point.get('clipped_to_retained_audio'):
        raise ValueError('A linked guide was clipped by the retained audio. Choose another guide or detach the snap link.')
    return point


def resolve(layer, target):
    link = binding(layer)
    own = guide(layer, link['own_guide_id'])
    other = guide(target, link['target_guide_id'])
    start = target['start_sample'] + other['sample'] - own['sample'] + link['offset_samples']
    if start < 0:
        raise ValueError('This snap link would place audio before the timeline start. Adjust its offset or detach it.')
    evidence = {'target_layer_id':target['id'], 'target_guide':copy.deepcopy(other),
                'own_guide':copy.deepcopy(own), 'offset_samples':link['offset_samples'],
                'target_guide_timeline_sample':target['start_sample'] + other['sample'],
                'own_guide_timeline_sample':start + own['sample'],
                'measured_after_processing':True, 'phoneme_accuracy_claimed':False}
    return start, evidence


def detach(layer, reason, *, start=None):
    """Keep a measured position when an authored graph operation removes a link."""
    link = binding(layer)
    if link is None:
        layer.pop('guide_binding', None)
        return
    position = layer.get('start_sample') if start is None else start
    if type(position) is not int or position < 0:
        raise ValueError('Measure a linked clip before detaching its timing relationship.')
    layer.pop('guide_binding', None);layer.pop('guide_binding_resolution', None)
    layer.update(after_layer_id=None, anchor_start_sample=position, gap_samples=0, overlap_samples=0)
    layer['guide_binding_detached'] = {'reason':reason, 'binding':copy.deepcopy(link), 'start_sample':position}


def detach_touching(layers, ids, reason):
    for layer in layers:
        link = binding(layer)
        if link and (layer['id'] in ids or link['target_layer_id'] in ids):detach(layer, reason)


def delivery_guides(layer, count, audio_sha256=None):
    """Keep authored guide coordinates; measure delivery edges from its decoder.

    Gain/encoding does not redefine a chosen phonetic candidate. The packet
    retains its original pre-encode analysis identity and explicitly records
    the delivery clock. A shortened delivery never turns a missing body guide
    into a valid endpoint.
    """
    if type(count) is not int or count < 0:raise ValueError('Invalid delivered clip length.')
    packet = copy.deepcopy(layer.get('snap_points'))
    if not isinstance(packet, dict):return packet
    original = layer.get('rendered_sample_count')
    if packet.get('sample_count') != original or packet.get('sample_rate') != RATE:
        raise ValueError('Delivery needs the current rendered clip guides.')
    packet.update(sample_count=count, analysis_sample_count=original,
                  delivered_audio_sha256=audio_sha256,
                  delivery_coordinate_policy='Pre-encode guide positions retained; file edges use measured delivered frames. No post-codec phoneme alignment.')
    for point in packet.get('points', []):
        if point['id'] == 'clip_start':point['sample'] = 0
        elif point['id'] == 'clip_end':point['sample'] = count
        elif point['sample'] > count:
            point.update(pre_delivery_sample=point['sample'], sample=count, clipped_to_retained_audio=True)
    return packet
