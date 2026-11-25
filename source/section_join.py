"""Reunite adjacent sections of one processed recording without rendering new media."""
import copy
import re

# Controls that must describe the same processed recording on both sides.
PROCESSING_DEFAULTS = {
    'playback_speed': 1, 'gain_db': 0, 'muted': False,
    'effects': [], 'bypass_track_effects': False, 'section_effects': [], 'section_history': [], 'group_section_effects': [],
    'source_mute_windows': [], 'source_mute_clock_rate': 44100,
    'clip_role': None,
}


def join_next(layers, layer):
    if layer.get('rendered_region') or layer.get('section_history') or layer.get('section_effects') or layer.get('group_section_effects') or any(
            item.get('rendered_region') and item.get('track_id') == layer.get('track_id')
            and item.get('start_sample') == layer.get('end_sample') for item in layers):
        from rendered_regions import join_regions
        return join_regions(layers,layer)
    context = layer.get('processing_context')
    if layer.get('kind') != 'audio' or not isinstance(context, dict):
        raise ValueError('Join adjacent sections created by splitting the same recording.')
    candidates = [item for item in layers if item['id'] != layer['id']
                  and item['track_id'] == layer['track_id']
                  and item.get('kind') == 'audio'
                  and item.get('asset_id') == layer.get('asset_id')
                  and item.get('slot_order') == layer.get('slot_order')
                  and item.get('source_in_sample') == layer.get('source_out_sample')
                  and item.get('start_sample') == layer.get('end_sample')]
    if len(candidates) != 1:
        raise ValueError('Choose sections with adjacent source ranges and no timeline gap or overlap.')
    second = candidates[0]
    if context != second.get('processing_context') or any(
            layer.get(key, default) != second.get(key, default)
            for key, default in PROCESSING_DEFAULTS.items()):
        raise ValueError('The sections have different processing settings. Match them before joining.')
    if layer.get('fade_out_samples', 0) or second.get('fade_in_samples', 0):
        raise ValueError('These sections have fades at the join. Keep them separate to retain those fades.')
    if second.get('after_layer_id') not in (None, layer['id']):
        raise ValueError('The next section follows another clip. Snap it after this section before joining.')
    if any(item.get('after_layer_id') == layer['id'] and item['id'] != second['id'] for item in layers):
        raise ValueError('Other clips follow this section boundary. Keep it to preserve their timing.')
    # Keep the first section's identity and anchor, and the last section's tail.
    layer['source_out_sample'] = second['source_out_sample']
    layer['fade_out_samples'] = second.get('fade_out_samples', 0)
    layer['label'] = re.sub(r'(?: · \d+)+$', '', layer.get('label') or layer.get('slot', {}).get('spoken_text') or 'Clip')
    for item in layers:
        if item.get('after_layer_id') == second['id']:
            item['after_layer_id'] = layer['id']
    layers.remove(second)
    # This is still a source section, including when it covers the whole source.
    # Keeping context also preserves any prior interior section envelopes.
    layer['processing_context'] = copy.deepcopy(context)
    return layers


def _processed_section(layer):
    """Express either section format on the original processed sample clock.

    Keep all source/effect controls and envelope history; no new media is baked.
    The renderer supplies exact offsets, including tempo rounding and FX tails.
    """
    from rendered_regions import bounds
    from section_effects import active
    if active(layer.get('section_effects')) or active(layer.get('group_section_effects')):
        raise ValueError('These sections have independent section effects. Keep them separate to preserve their sound.')
    if layer.get('section_history'):
        result=copy.deepcopy(layer)
        first,stop=bounds(layer)
        result.setdefault('rendered_region',{'start_sample':first,'end_sample':stop})
        return result
    origin = layer.get('section_origin')
    fields = ('source_in_sample', 'source_out_sample', 'fade_in_samples',
              'fade_out_samples', 'dry_sample_count', 'sample_count',
              'section_start_sample', 'section_end_sample')
    if not isinstance(origin, dict) or any(type(origin.get(key)) is not int for key in fields):
        raise ValueError('Render these sections again before joining their processed audio.')
    first, stop = origin['section_start_sample'], origin['section_end_sample']
    if not 0 <= first < stop <= origin['sample_count']:
        raise ValueError('Invalid processed section timing. Render the sections again.')
    result = copy.deepcopy(layer)
    context = layer.get('processing_context') or {}
    envelopes = []
    # Current interior source-section fades occur after its full-source rack.
    # Preserve their original window when converting to processed coordinates.
    fade_in = layer.get('fade_in_samples', 0) if layer['source_in_sample'] != origin['source_in_sample'] else 0
    fade_out = layer.get('fade_out_samples', 0) if layer['source_out_sample'] != origin['source_out_sample'] else 0
    if fade_in or fade_out:
        envelopes.append({'start_sample': first, 'end_sample': stop,
                          'fade_in_samples': fade_in, 'fade_out_samples': fade_out})
    for old in layer.get('rendered_envelopes', []):
        item = copy.deepcopy(old)
        item['start_sample'] += first
        item['end_sample'] += first
        envelopes.append(item)
    region = layer.get('rendered_region')
    left, right = bounds(layer)
    result['rendered_region'] = {'start_sample': first+left, 'end_sample': first+right,
                                'fade_in_samples': region.get('fade_in_samples', 0) if region else 0,
                                'fade_out_samples': region.get('fade_out_samples', 0) if region else 0}
    if region and region.get('follow_end') and first+right==origin['sample_count']:result['rendered_region']['follow_end']=True
    result['rendered_envelopes'] = envelopes
    for key in ('source_in_sample', 'source_out_sample', 'fade_in_samples', 'fade_out_samples'):
        result[key] = origin[key]
    result['processing_context'] = {key: origin[key] for key in
                                   ('source_in_sample', 'source_out_sample', 'fade_in_samples', 'fade_out_samples')}
    if context.get('section_envelopes'):
        result['processing_context']['section_envelopes'] = copy.deepcopy(context['section_envelopes'])
    return result


def join_processed_sections(layers, layer):
    """Join matching source and rendered sections without changing their signal."""
    from rendered_regions import bounds
    first = _processed_section(layer)
    candidates = []
    for item in layers:
        if (item['id'] == layer['id'] or item.get('kind') != 'audio'
                or item.get('track_id') != layer.get('track_id')
                or item.get('asset_id') != layer.get('asset_id')
                or item.get('slot_order') != layer.get('slot_order')
                or item.get('start_sample') != layer.get('end_sample')):
            continue
        candidate = _processed_section(item)
        if bounds(candidate)[0] == bounds(first)[1]:
            candidates.append((item, candidate))
    if len(candidates) != 1:
        raise ValueError('Join adjacent sections of the same recording with no timeline gap.')
    second, normalized = candidates[0]
    same = dict(PROCESSING_DEFAULTS, source_in_sample=0, source_out_sample=None,
                processing_context=None, fade_in_samples=0, fade_out_samples=0,
                rendered_envelopes=[])
    if any(first.get(key, default) != normalized.get(key, default) for key, default in same.items()):
        raise ValueError('The sections have different processing settings. Match them before joining.')
    if (second.get('after_layer_id') not in (None, layer['id'])
            or second.get('gap_samples', 0) or second.get('overlap_samples', 0)):
        raise ValueError('Other timing links use this boundary. Keep the sections separate.')
    # A gap-preserving Cut may still link the retained suffix to the first
    # section across the empty interval. Filling/joining that interval consumes
    # its gap; keep that suffix and its descendants at their original positions.
    added = second['end_sample'] - layer['end_sample']
    followers = [item for item in layers if item.get('after_layer_id') == layer['id'] and item['id'] != second['id']]
    if any(item.get('overlap_samples', 0) or item.get('gap_samples', 0) < added
           or item['start_sample'] < second['end_sample'] for item in followers):
        raise ValueError('Other timing links use this boundary. Keep the sections separate.')
    if first['rendered_region'].get('fade_out_samples') or normalized['rendered_region'].get('fade_in_samples'):
        raise ValueError('These sections have fades at the join. Keep them separate to retain those fades.')
    # Each edge fade was limited by its original section length. Extending the
    # joined window must not lengthen a fade that previously filled that section.
    first['rendered_region']['fade_in_samples'] = min(first['rendered_region'].get('fade_in_samples', 0), layer['rendered_sample_count'])
    first['rendered_region']['end_sample'] = bounds(normalized)[1]
    if normalized['rendered_region'].get('follow_end'):first['rendered_region']['follow_end']=True
    else:first['rendered_region'].pop('follow_end',None)
    first['rendered_region']['fade_out_samples'] = min(normalized['rendered_region'].get('fade_out_samples', 0), second['rendered_sample_count'])
    first['label'] = re.sub(r'(?: · \d+)+$', '', first.get('label') or first.get('slot', {}).get('spoken_text') or 'Clip')
    layer.update(first)
    for item in followers:
        item['gap_samples'] -= added
    for item in layers:
        if item.get('after_layer_id') == second['id']:
            item['after_layer_id'] = layer['id']
    layers.remove(second)
    return layers
