"""Sample-clock alignment for independent replacement recordings."""

import copy
import hashlib
from collections import Counter

from contextual_packet_layout import packet_for_layer, packet_slices
from engine_limits import MAX_AUDIO_SECONDS
from guide_bindings import RATE, binding, guide, resolve

FAMILIES = {
    'edges': [('clip_end', 'clip_start')],
    'provider': [('provider_end', 'provider_start')],
    'returned': [('returned_alignment_end', 'returned_alignment_start'),
                 ('returned_generation_end', 'returned_generation_start')],
    'activity': [('decay', 'leader')],
}
FAMILIES['all'] = FAMILIES['provider'] + FAMILIES['returned'] + FAMILIES['activity']

MIN_PAUSE_SHORTENING = round(.020 * RATE)


def choice_key(layer, policy):
    if policy.get('context_choice') is not None:
        return 'choice:' + policy['context_choice']
    return 'asset:' + layer['asset_id'] if layer.get('asset_id') else None


def _contextual_group(base, layer):
    packet = packet_for_layer(base, layer['id'])
    if packet:
        return (base.get('contextual_choices') or {}).get(packet['region_id'])
    return None


def _offsets(value):
    if (not isinstance(value, dict) or not value or set(value) - {'lead', 'tail'}
            or any(type(n) is not int or abs(n) > MAX_AUDIO_SECONDS * RATE for n in value.values())):
        raise ValueError('Enter a valid signed offset for at least one replacement join.')
    return value


def _set_pause_sides(policy, key, sides):
    """Record which saved offsets were measured against the source."""
    matches = policy.get('source_pause_sides', {})
    if sides:
        policy.setdefault('source_pause_sides', {})[key] = sorted(sides)
    elif key in matches:
        del matches[key]
        if not matches:
            del policy['source_pause_sides']


def requested_mode(operation):
    mode = operation.get('snap_mode')
    if mode is not None and (not isinstance(mode, str) or mode not in FAMILIES):
        raise ValueError('Choose an available snap guide family.')
    return mode


def select(layer, operation, *, contextual_choice=None):
    if 'snap_mode' in operation:
        layer.setdefault('replacement_snap', {})['mode'] = requested_mode(operation)
    if contextual_choice is not None:
        layer.setdefault('replacement_snap', {})['context_choice'] = contextual_choice


def select_for_delimiter(layer, record, operation, *, contextual_choice=None, definitions=None):
    """Use the corpus default above the timeline setting; keep saved take overrides."""
    if definitions is None:
        from delimiter_definitions import registered
        definitions = {item['key']: item for item in registered(record)}
    mode = definitions.get(layer.get('slot', {}).get('type'), {}).get('snap_mode')
    if mode is not None:
        operation = {**operation, 'snap_mode': None if mode == 'off' else mode}
    select(layer, operation, contextual_choice=contextual_choice)


def carry_recording_settings(previous, layout):
    """Keep per-recording snap edits only where the authored cut is unchanged."""
    old_assembly = previous['contextual_assembly']
    new_assembly = layout['provenance']
    if old_assembly.get('source_sha256') != new_assembly.get('source_sha256'):
        return []
    same_base_mode = (old_assembly.get('preserve_base') is True) == (new_assembly.get('preserve_base') is True)
    old_packets = {packet['region_id']: packet for packet in old_assembly['packets']}
    new_packets = {packet['region_id']: packet for packet in new_assembly['packets']}
    old_layers = {layer['id']: layer for layer in previous['layers']}
    new_layers = {layer['id']: layer for layer in layout['layers']}
    old_groups = previous.get('contextual_choices', {})
    review = {}
    policy_fields = ('modes', 'join_modes', 'overrides', 'source_pause_sides')

    def signature(choice):
        return (choice['asset_id'], choice['sha256'], choice['value'],
                tuple(choice['returned_region']), tuple(choice['word_bounds']))

    def clean_dependency(old, new):
        return (old is not None and new is not None
                and old.get('after_layer_id') == new.get('after_layer_id')
                and not (old.get('guide_binding') or old.get('gap_samples') or old.get('overlap_samples')
                         or old.get('rendered_region') or old.get('track_bus_id'))
                and old.get('playback_speed', 1) == 1
                and type(old.get('source_out_sample')) is int
                and type(old.get('source_in_sample')) is int
                and old.get('rendered_sample_count') == old['source_out_sample'] - old['source_in_sample'])

    for region, new_group in layout['choices'].items():
        if region not in old_groups:
            continue
        old_packet, new_packet = old_packets[region], new_packets[region]
        old_group = old_groups[region]
        selected = old_group['choices'][old_group['selected']]
        start, stop = selected['returned_region']
        slices = packet_slices(region, (start, *selected['word_bounds'], stop))
        new_takes = {(choice['asset_id'], choice['value']) for choice in new_group['choices'].values()}
        for ident in slices:
            policy = old_layers.get(ident, {}).get('replacement_snap', {})
            for field in policy_fields:
                for key in policy.get(field, {}):
                    choice = old_group['choices'].get(key[7:]) if key.startswith('choice:') else None
                    if choice and (choice['asset_id'], choice['value']) in new_takes:
                        review[(ident, key)] = (new_packet['slot_order'], choice['value'])
        if not same_base_mode or old_packet['source_region'] != new_packet['source_region']:
            continue
        if any(not clean_dependency(old_layers.get(ident), new_layers.get(ident))
               or old_layers[ident]['asset_id'] != selected['asset_id']
               or (old_layers[ident]['source_in_sample'], old_layers[ident]['source_out_sample']) != bounds
               for ident, bounds in slices.items()):
            continue
        # If another occurrence changed the neighboring source clips, don't
        # apply offsets that were authored against the previous handles.
        first, last = next(iter(slices)), next(reversed(slices))
        lead = new_layers[first].get('after_layer_id')
        tails = {item['id'] for item in new_layers.values() if item.get('after_layer_id') == last}
        if tails != {item['id'] for item in old_layers.values() if item.get('after_layer_id') == last}:
            continue
        neighbors = tails | ({lead} if lead else set())
        if any(not clean_dependency(old_layers.get(ident), new_layers.get(ident))
               or any(old_layers[ident].get(field) != new_layers[ident].get(field) for field in (
                   'asset_id', 'source_in_sample', 'source_out_sample')) for ident in neighbors):
            continue
        old_signatures = {key: signature(choice) for key, choice in old_group['choices'].items()}
        new_signatures = {key: signature(choice) for key, choice in new_group['choices'].items()}
        old_counts = Counter(old_signatures.values())
        new_counts = Counter(new_signatures.values())
        old_keys = {sig: key for key, sig in old_signatures.items() if old_counts[sig] == 1}
        keys = {f'choice:{old_keys[sig]}': f'choice:{key}'
                for key, sig in new_signatures.items() if sig in old_keys and new_counts[sig] == 1}
        for ident in slices:
            old_policy = old_layers[ident].get('replacement_snap', {})
            fields = {}
            for field in policy_fields:
                edits = {keys[key]: copy.deepcopy(value) for key, value in old_policy.get(field, {}).items()
                         if key in keys}
                if edits:
                    fields[field] = edits
            if fields:
                policy = new_layers[ident].setdefault('replacement_snap', {})
                policy.update(fields, context_choice=new_group['selected'])
                for key in keys:
                    review.pop((ident, key), None)
    return [{'slot_order': order, 'value': value} for order, value in sorted(set(review.values()))]


def _natural_join(prior, layer):
    return (layer.get('after_layer_id') == prior['id'] and not layer.get('guide_binding')
            and not layer.get('gap_samples') and not layer.get('overlap_samples')
            and all(clip.get('kind') == 'audio' and not clip.get('clip_role')
                    and not clip.get('slot', {}).get('section_id') for clip in (prior, layer)))


def _continuous_context(prior, layer):
    """Identify adjacent, unretimed slices from one contextual recording."""
    return (any(clip['id'].startswith('packet:') for clip in (prior, layer))
            and prior.get('asset_id') and prior['asset_id'] == layer.get('asset_id')
            and prior.get('source_out_sample') == layer.get('source_in_sample', 0)
            and all(clip.get('playback_speed', 1) == 1
                    and not clip.get('rendered_region')
                    and clip.get('source_out_sample', 0) - clip.get('source_in_sample', 0)
                    == clip['rendered_sample_count']
                    for clip in (prior, layer)))


def automatic_join_sides(layers, layer):
    return {'lead': any(_natural_join(item, layer) for item in layers),
            'tail': any(_natural_join(layer, item) for item in layers)}


def recording_key(base, layer):
    group = _contextual_group(base, layer)
    return 'choice:' + group['selected'] if group else choice_key(layer, layer.get('replacement_snap', {}))


def _is_unsplit_replacement(layer, layers):
    return (layer.get('kind') == 'audio'
            and layer.get('slot', {}).get('type') not in (None, 'fixed')
            and sum(item.get('slot_order') == layer.get('slot_order') and
                    item.get('slot', {}).get('type') not in (None, 'fixed') for item in layers) == 1)


def _replacement_layer(base, operation):
    layers = copy.deepcopy(base['layers'])
    layer = next((item for item in layers if item['id'] == operation.get('layer_id')), None)
    if layer is None or not _is_unsplit_replacement(layer, layers):
        raise ValueError('Choose an unsplit replacement recording.')
    return layers, layer


def edit(base, operation):
    layers, layer = _replacement_layer(base, operation)
    policy = layer.setdefault('replacement_snap', {})
    group = _contextual_group(base, layer)
    contextual_choice = group['selected'] if group else None
    if 'snap_mode' in operation:
        select(layer, operation, contextual_choice=contextual_choice)
    elif contextual_choice is not None:
        policy['context_choice'] = contextual_choice
    key = choice_key(layer, policy)
    offsets = operation.get('offsets')
    overrides = policy.setdefault('overrides', {})
    measured = policy.get('source_pause_sides', {}).get(key, ())
    if offsets is None:
        overrides.pop(key, None)
        _set_pause_sides(policy, key, ())
    else:
        offsets = _offsets(offsets)
        neighbors = automatic_join_sides(layers, layer)
        if any(not neighbors[side] for side in offsets):
            raise ValueError('This side needs a neighboring clip with an automatic timing dependency.')
        previous = overrides.get(key, {})
        _set_pause_sides(policy, key, [side for side in measured if side in offsets and offsets[side] == previous.get(side)])
        overrides[key] = offsets.copy()
    return layers


def _outer_context_join(base, prior, incoming):
    assembly = base.get('contextual_assembly') or {}
    if (assembly.get('format') != 'dmspeaks-context-packets-1'
            or any(clip.get('slot', {}).get('type') != 'fixed' for clip in (prior, incoming))
            or _continuous_context(prior, incoming)):
        return False
    for packet in assembly.get('packets', ()):
        region = packet['region_id']
        prefix = f'packet:{len(region)}:{region}:'
        if prior['id'] == prefix + 'after' or incoming['id'] == prefix + 'before':
            return True
    return False


def _edit_linked_join(layers, incoming, prior, link, mode, *, new_link=False):
    """Change the guide pair while keeping the link target and its authored offset."""
    candidates = []
    for target_id, own_id in FAMILIES[mode]:
        proposed = {**link, 'target_guide_id': target_id, 'own_guide_id': own_id}
        try:
            start, evidence = resolve({**incoming, 'after_layer_id': None, 'guide_binding': proposed}, prior)
        except ValueError:
            continue
        if evidence['target_guide'].get('timing_partial') or evidence['own_guide'].get('timing_partial'):
            continue
        if new_link and start < prior['start_sample']:
            continue
        candidates.append((abs(start - incoming['start_sample']), proposed))
    if not candidates:
        raise ValueError('This guide family has no usable pair for the link. Choose another family.')
    selected = min(candidates, key=lambda candidate: candidate[0])[1]
    return [{**layer, 'after_layer_id': None, 'guide_binding': selected}
            if layer['id'] == incoming['id'] else layer
            for layer in copy.deepcopy(layers)]


def _source_pause_packet(base, source, donor, side):
    if ((base.get('contextual_assembly') or {}).get('format') != 'dmspeaks-context-packets-1'
            or not source['id'].startswith('source-core:')):
        return None
    prefix, _, _ = donor['id'].rpartition(':')
    packet = packet_for_layer(base, prefix + ':word')
    if not packet or not (base.get('contextual_choices') or {}).get(packet['region_id']):
        return None
    lo, hi = packet['returned_region']
    slices = packet_slices(packet['region_id'], (lo, *packet['word_bounds'], hi))
    if side == 'lead':
        boundary = next(iter(slices))
        source_edge = source.get('source_out_sample')
        donor_edge = donor.get('source_in_sample')
        index = 0
    else:
        boundary = next(reversed(slices))
        source_edge = source.get('source_in_sample')
        donor_edge = donor.get('source_out_sample')
        index = 1
    if (donor['id'] != boundary or source_edge != packet['source_region'][index]
            or donor_edge != packet['returned_region'][index]):
        return None
    return packet


def _quiet_source_padding(app, record, base, source_layer, donor_layer, packet, side, cache=None):
    """Measure the quiet portion at the same edge of the source and return."""
    from contextual_return import checked_pcm
    from timed_contextual_packets import quiet_edge_padding

    source = app.store.asset(source_layer['asset_id'])
    if source.get('sha256') != base['contextual_assembly']['source_sha256']:
        raise ValueError('The original source recording is unavailable at this join.')
    donor = app.store.asset(donor_layer['asset_id'])
    if donor.get('sha256') != packet['context_sha256']:
        raise ValueError('The replacement recording differs from the saved context.')
    def pcm(asset):
        if cache is None:
            return checked_pcm(app, record, asset)
        if asset['id'] not in cache:
            if len(cache) >= 4:
                del cache[next(iter(cache))]
            cache[asset['id']] = checked_pcm(app, record, asset)
        return cache[asset['id']]

    returned = pcm(donor)
    start, end = donor_layer['source_in_sample'], donor_layer['source_out_sample']
    lo, hi = packet['returned_region']
    slices = packet_slices(packet['region_id'], (lo, *packet['word_bounds'], hi))
    if (start, end) != slices.get(donor_layer['id']):
        raise ValueError('Restore the original replacement boundaries before matching the source pause.')
    clip = returned[start:end]
    digest = hashlib.sha256(memoryview(clip).cast('B')).hexdigest()
    if digest != donor_layer.get('snap_points', {}).get('analysis_pcm_sha256'):
        raise ValueError('Match Source Pause needs the unprocessed replacement audio.')
    original = pcm(source)
    lo, hi = packet['source_region']
    context_only = not donor_layer['id'].endswith(':word')
    if side == 'lead':
        source_quiet = quiet_edge_padding(original, lo, hi, -55)
        replacement_quiet = quiet_edge_padding(returned, start, end, -55)
        if (not source_quiet < hi - lo or not replacement_quiet
                or replacement_quiet == end - start and not context_only):
            raise ValueError('The replacement needs a measurable quiet lead-in before speech.')
    else:
        source_quiet = quiet_edge_padding(original, hi, lo, -55)
        replacement_quiet = quiet_edge_padding(returned, end, start, -55)
        if (not 0 < source_quiet < hi - lo or not replacement_quiet
                or replacement_quiet == end - start and not context_only):
            raise ValueError('Both recordings need a measurable quiet tail after speech.')
    return source_quiet, replacement_quiet


def _source_pause_adjustment(app, record, base, prior, incoming, cache=None):
    """Measure one safe offset against the original contextual recording."""
    if not _natural_join(prior, incoming):
        raise ValueError('Match Source Pause needs an automatic contextual join.')
    lead_packet = _source_pause_packet(base, prior, incoming, 'lead')
    if lead_packet:
        source_quiet, replacement_quiet = _quiet_source_padding(
            app, record, base, prior, incoming, lead_packet, 'lead', cache)
        available = prior['rendered_sample_count']
        excess = min(available, replacement_quiet - max(source_quiet, round(.030 * RATE)))
        if excess < MIN_PAUSE_SHORTENING:
            raise ValueError('This recording has no extra quiet lead-in to shorten here.')
        return incoming['id'], lead_packet['region_id'], 'lead', -excess

    packet = _source_pause_packet(base, incoming, prior, 'tail')
    if (not packet
            or sum(layer.get('slot_order') == packet['slot_order'] and
                   layer.get('slot', {}).get('type') not in (None, 'fixed') for layer in base['layers']) != 1
            or sum(_natural_join(prior, layer) for layer in base['layers']) != 1):
        raise ValueError('Match Source Pause needs a contextual replacement beside its original source clip.')
    source_quiet, replacement_quiet = _quiet_source_padding(
        app, record, base, incoming, prior, packet, 'tail', cache)
    excess = replacement_quiet - max(source_quiet, round(.030 * RATE))
    if excess < MIN_PAUSE_SHORTENING:
        raise ValueError('This recording has no extra quiet tail to shorten.')
    return prior['id'], packet['region_id'], 'tail', -excess


def _save_pause_adjustment(base, layers, adjustment):
    owner, region, side, offset = adjustment
    policy = layers[owner].setdefault('replacement_snap', {})
    policy['context_choice'] = base['contextual_choices'][region]['selected']
    key = choice_key(layers[owner], policy)
    policy.setdefault('overrides', {}).setdefault(key, {})[side] = offset
    _set_pause_sides(policy, key, set(policy.get('source_pause_sides', {}).get(key, ())) | {side})


def _match_source_pause(app, record, base, prior, incoming):
    adjustment = _source_pause_adjustment(app, record, base, prior, incoming)
    current_spacing = incoming['start_sample'] - prior['end_sample']
    if current_spacing - adjustment[-1] < MIN_PAUSE_SHORTENING:
        raise ValueError('The current pause is already this short. Choose a different timing to compare.')
    layers = copy.deepcopy(base['layers'])
    _save_pause_adjustment(base, {layer['id']: layer for layer in layers}, adjustment)
    return layers


def match_dialogue_pauses(app, record, base):
    """Suggest measured pause offsets for the currently selected recordings."""
    from contextual_choices import FORMAT
    if ((base.get('contextual_assembly') or {}).get('format') != FORMAT
            or not base.get('contextual_choices') or base.get('replacement_playback')
            or base.get('processing_definition') is not None):
        raise ValueError('Open the selected contextual mix to match its source pauses.')
    original = {layer['id']: layer for layer in base['layers']}
    layers = copy.deepcopy(base['layers'])
    edited = {layer['id']: layer for layer in layers}
    tracks = {track['id']: track for track in base['tracks']}
    cache, adjustments = {}, []

    def has_saved_timing(owner, side):
        policy = owner.get('replacement_snap', {})
        key = recording_key(base, owner)
        return (side in policy.get('overrides', {}).get(key, {})
                or side in policy.get('join_modes', {}).get(key, {})
                or key in policy.get('modes', {}))

    for incoming in base['layers']:
        prior = original.get(incoming.get('after_layer_id'))
        if not prior or not _natural_join(prior, incoming):
            continue
        if any(tracks[clip['track_id']].get('group_locked') or
               not clip.get('rendered_sample_count') for clip in (prior, incoming)):
            continue
        if has_saved_timing(prior, 'tail') or has_saved_timing(incoming, 'lead'):
            continue
        try:
            adjustment = _source_pause_adjustment(app, record, base, prior, incoming, cache)
        except ValueError:
            continue
        offset = adjustment[-1]
        current_spacing = incoming['start_sample'] - prior['end_sample']
        shortened = current_spacing - offset
        if shortened < MIN_PAUSE_SHORTENING:
            continue
        _save_pause_adjustment(base, edited, adjustment)
        adjustments.append({'join_layer_id': incoming['id'], 'offset_samples': offset,
                            'shortened_samples': shortened})
    if not adjustments:
        raise ValueError('No extra quiet pauses could be matched in the selected recordings.')
    return layers, adjustments


def _validate_pause_match_quality(base, result):
    if result['sample_count'] > base['sample_count']:
        raise ValueError('The matched mix would run longer. Review the joins individually.')
    before = {layer['id']: layer for layer in base['layers']}
    for layer in result['layers']:
        previous = before[layer['id']]
        for field, metric in (('join_edge_warning', 'step'), ('join_activity_warning', 'shared_samples')):
            if layer.get(field, {}).get(metric, 0) > previous.get(field, {}).get(metric, 0):
                raise ValueError('The matched mix introduced a possible click or active overlap. Compare another timing.')


def validate_join_pause_match(base, result, join_id):
    """Check that a measured offset shortened the intended join in the actual mix."""
    before = {layer['id']: layer for layer in base['layers']}
    after = {layer['id']: layer for layer in result['layers']}
    parent_id = before[join_id]['after_layer_id']
    old_spacing = before[join_id]['start_sample'] - before[parent_id]['end_sample']
    new_spacing = after[join_id]['start_sample'] - after[parent_id]['end_sample']
    resolution = after[join_id].get('replacement_snap_resolution', {})
    if (resolution.get('offset_origin') != 'source_pause'
            or old_spacing - new_spacing < MIN_PAUSE_SHORTENING):
        raise ValueError('The measured pause could not be shortened at this join.')
    _validate_pause_match_quality(base, result)


def validate_dialogue_pause_match(base, result, adjustments):
    after = {layer['id']: layer for layer in result['layers']}
    for item in adjustments:
        join = after[item['join_layer_id']].get('replacement_snap_resolution', {})
        if join.get('offset_origin') != 'source_pause' or join.get('offset_samples') != item['offset_samples']:
            raise ValueError('A measured pause could not be applied. Review its join individually.')
    _validate_pause_match_quality(base, result)


def _restore_join_defaults(base, prior, incoming, record=None):
    """Clear only the two clip sides that own this join, keeping other joins intact."""
    layers = copy.deepcopy(base['layers'])
    by_id = {layer['id']: layer for layer in layers}
    outer_context = _outer_context_join(base, prior, incoming)
    changed = False
    for owner, side in ((incoming, 'lead'), (prior, 'tail')):
        layer = by_id[owner['id']]
        source = prior if side == 'lead' else incoming
        managed_edge = outer_context and _context_edge_owner(layer, side, by_id) is not layer
        if not managed_edge and not _source_pause_packet(base, source, owner, side):
            try:
                _replacement_layer(base, {'layer_id': owner['id']})
            except ValueError:
                continue
        policy = layer.get('replacement_snap', {})
        if record is not None and layer.get('slot', {}).get('type') != 'fixed':
            previous_mode = policy.get('mode')
            select_for_delimiter(layer, record, {'snap_mode': previous_mode})
            policy = layer['replacement_snap']
            changed = changed or policy.get('mode') != previous_mode
        key = recording_key(base, layer)
        for field in ('overrides', 'join_modes'):
            settings = policy.get(field, {})
            sides = settings.get(key, {})
            if side not in sides:
                continue
            del sides[side]
            changed = True
            if field == 'overrides':
                measured = policy.get('source_pause_sides', {}).get(key, ())
                _set_pause_sides(policy, key, [saved_side for saved_side in measured if saved_side != side])
            if not sides:
                del settings[key]
            if not settings:
                del policy[field]
    if not changed:
        raise ValueError('This join already follows the recording default.')
    return layers


def _edit_join_offset(base, prior, incoming, offset):
    """Save one clip-edge offset on the selected recording."""
    _offsets({'lead': offset})
    if _continuous_context(prior, incoming):
        raise ValueError('Choose the outer edge of this recording to adjust its timing.')
    candidates = [(incoming, prior, 'lead'), (prior, incoming, 'tail')]
    current_owner = incoming.get('replacement_snap_resolution', {}).get('owner_layer_id')
    if current_owner == prior['id']:
        candidates.reverse()
    for owner, source, side in candidates:
        packet = _source_pause_packet(base, source, owner, side)
        if not packet and not _is_unsplit_replacement(owner, base['layers']):
            continue
        if side == 'tail' and sum(_natural_join(owner, layer) for layer in base['layers']) != 1:
            continue
        if incoming.get('replacement_snap_resolution', {}).get('mode') == 'override' and current_owner != owner['id']:
            continue
        group = base['contextual_choices'][packet['region_id']] if packet else _contextual_group(base, owner)
        key = 'choice:' + group['selected'] if group else recording_key(base, owner)
        if not key:
            continue
        layers = copy.deepcopy(base['layers'])
        layer = next(item for item in layers if item['id'] == owner['id'])
        policy = layer.setdefault('replacement_snap', {})
        if group:
            policy['context_choice'] = group['selected']
        policy.setdefault('overrides', {}).setdefault(key, {})[side] = offset
        measured = policy.get('source_pause_sides', {}).get(key, ())
        _set_pause_sides(policy, key, set(measured) - {side})
        return layers
    raise ValueError('Choose an automatic join owned by a single replacement recording.')


def edit_join_family(base, operation, *, app=None, record=None):
    """Choose a guide family for an automatic replacement or an authored link."""
    join_layer_id = operation.get('join_layer_id')
    if not isinstance(join_layer_id, str):
        raise ValueError('Choose an existing join.')
    mode = operation.get('snap_mode')
    if not isinstance(mode, str) or mode not in (*FAMILIES, 'source_pause', 'recording_default', 'offset'):
        raise ValueError('Choose an available snap guide family.')
    by_id = {layer['id']: layer for layer in base['layers']}
    incoming = by_id.get(join_layer_id)
    link = binding(incoming) if incoming else None
    target_id = link['target_layer_id'] if link else (incoming or {}).get('after_layer_id')
    prior = by_id.get(target_id)
    if not prior or any(clip.get('kind') != 'audio' or not clip.get('rendered_sample_count')
                        for clip in (prior, incoming)):
        raise ValueError('Choose a join between two audible clips.')
    if mode == 'source_pause':
        if app is None or record is None:
            raise ValueError('Open the saved dialogue before matching its source pause.')
        return _match_source_pause(app, record, base, prior, incoming)
    if link:
        if mode == 'recording_default':
            raise ValueError('Detach the authored guide link to follow recording defaults.')
        if mode == 'offset':
            raise ValueError('Edit the authored guide link to change its offset.')
        return _edit_linked_join(base['layers'], incoming, prior, link, mode)
    if not _natural_join(prior, incoming):
        raise ValueError('Choose a guide link or an automatic replacement join.')
    if mode == 'recording_default':
        return _restore_join_defaults(base, prior, incoming, record)
    if mode == 'offset':
        return _edit_join_offset(base, prior, incoming, operation.get('offset_samples'))
    resolution = incoming.get('replacement_snap_resolution', {})
    if resolution.get('mode') == 'override':
        raise ValueError('Clear the active custom offset before comparing guide families.')
    if _outer_context_join(base, prior, incoming):
        for edge, side in ((incoming, 'lead'), (prior, 'tail')):
            word = _context_edge_owner(edge, side, by_id)
            if word is edge:
                continue
            layers = copy.deepcopy(base['layers'])
            owner = next(layer for layer in layers if layer['id'] == edge['id'])
            policy = owner.setdefault('replacement_snap', {})
            group = _contextual_group(base, word)
            if group:
                policy['context_choice'] = group['selected']
            key = choice_key(owner, policy)
            policy.setdefault('join_modes', {}).setdefault(key, {})[side] = mode
            return layers
        return _edit_linked_join(base['layers'], incoming, prior,
                                 {'target_layer_id': prior['id'], 'offset_samples': 0},
                                 mode, new_link=True)
    owners = [(incoming, 'lead'), (prior, 'tail')]
    if resolution.get('owner_layer_id') == prior['id']:
        owners.reverse()
    for owner, side in owners:
        try:
            layers, layer = _replacement_layer(base, {'layer_id':owner['id']})
        except ValueError:
            continue
        policy = layer.setdefault('replacement_snap', {})
        group = _contextual_group(base, layer)
        if group:
            policy['context_choice'] = group['selected']
        key = choice_key(layer, policy)
        if key:
            join_modes = policy.setdefault('join_modes', {})
            join_modes.setdefault(key, {})[side] = mode
            return layers
    raise ValueError('This join needs an unsplit replacement recording.')


def _match_mode_recordings(app, base, operation, layer):
    from contextual_choices import current_record_for

    record = current_record_for(app, base)
    values = operation.get('values')
    if (not isinstance(values, list) or not 1 <= len(values) <= 1000
            or any(not isinstance(value, str) or not value or len(value) > 1000 for value in values)):
        raise ValueError('Select one or more replacement values.')
    requested = set(values)
    group = _contextual_group(base, layer)
    if group:
        if any(track.get('group_locked') for track in base['tracks'] if track['id'] == 'base'):
            raise ValueError('Unlock the base track before editing contextual joins.')
        matches = _matching_contextual_recordings(app, record, group, requested)
    else:
        from track_candidates import context
        _, _, _, _, _, candidates = context(app, {**operation, 'asset_id': base['id']})
        matches = [('asset:' + ident, value) for ident, value in candidates.matching_values(values)]
    return sorted(matches)


def _matching_contextual_recordings(app, record, group, requested):
    from contextual_return import verify_saved_media

    verified = {}
    matches = []
    for key, choice in group['choices'].items():
        if choice['value'] not in requested:
            continue
        ident = choice['asset_id']
        if ident not in verified:
            try:
                asset = app.store.asset(ident)
                verify_saved_media(app, record, asset)
                verified[ident] = asset['sha256']
            except (KeyError, OSError, ValueError):
                verified[ident] = None
        if verified[ident] and verified[ident] == choice['sha256']:
            matches.append(('choice:' + key, choice['value']))
    return matches


def describe_mode_recordings(app, request):
    """Preview the exact recordings eligible for a snap-mode edit."""
    base = app.store.asset(request['asset_id'])
    if base.get('kind') == 'scoped-arrangement-preview' or base.get('replacement_playback'):
        raise ValueError('Open the dry arrangement before setting snap modes on saved recordings.')
    _, layer = _replacement_layer(base, request)
    matches = _match_mode_recordings(app, base, request, layer)
    found = {value for _, value in matches}
    return {'recordings': [key for key, _ in matches],
            'matched': list(dict.fromkeys(value for value in request['values'] if value in found)),
            'missing': list(dict.fromkeys(value for value in request['values'] if value not in found))}


def edit_modes(app, base, operation):
    """Set the guide family on exact saved recordings for one occurrence."""
    layers, layer = _replacement_layer(base, operation)
    mode = operation.get('snap_mode')
    if 'snap_mode' not in operation or mode is not None and (not isinstance(mode, str) or mode not in FAMILIES):
        raise ValueError('Choose an available snap guide family.')
    matches = _match_mode_recordings(app, base, operation, layer)
    if 'expected_recordings' in operation and operation['expected_recordings'] != [key for key, _ in matches]:
        raise ValueError('The saved recordings changed. Reopen Set Snap Mode to review the new matches.')
    missing = set(operation['values']).difference(value for _, value in matches)
    if missing:
        examples = ', '.join(sorted(missing)[:3])
        raise ValueError(f'No compatible recording for {examples}. Select only values with saved recordings.')
    policy = layer.setdefault('replacement_snap', {})
    group = _contextual_group(base, layer)
    if group:
        policy['context_choice'] = group['selected']
    modes = policy.setdefault('modes', {})
    for key, _ in matches:
        if mode is None:
            modes.pop(key, None)
        else:
            modes[key] = mode
    if not modes:
        policy.pop('modes')
    references(layer)
    return layers


def _saved_join_mode(layer, side):
    policy = layer.get('replacement_snap', {})
    return policy.get('join_modes', {}).get(choice_key(layer, policy), {}).get(side)


def _context_edge_owner(edge, side, layers):
    """Find the replacement whose retained context reaches this join."""
    prefix, _, role = edge['id'].rpartition(':')
    if not prefix.startswith('packet:') or role != ('before' if side == 'lead' else 'after'):
        return edge
    word = layers.get(prefix + ':word')
    if not word or not word.get('replacement_snap', {}).get('apply_to_context_edges'):
        return edge
    pair = (edge, word) if side == 'lead' else (word, edge)
    if _natural_join(*pair) and _continuous_context(*pair):
        return word
    return edge


def _quiet_overlap_start(prior, layer):
    """The earliest proposed join that keeps the measured activity apart."""
    try:
        decay = guide(prior, 'decay')['sample']
        leader = guide(layer, 'leader')['sample']
    except ValueError:
        return prior['end_sample']
    return prior['start_sample'] + decay - leader


def join(prior, layer, layers):
    """Resolve a natural join using freshly measured guides or a saved offset."""
    if not _natural_join(prior, layer) or not prior.get('rendered_sample_count') or not layer.get('rendered_sample_count'):
        return None
    owners = [(layer, 'lead'), (prior, 'tail')]
    for owner, side in owners:
        policy = owner.get('replacement_snap', {})
        key = choice_key(owner, policy)
        offsets = policy.get('overrides', {}).get(key, {})
        if side in offsets:
            start = prior['end_sample'] + offsets[side]
            if start < prior['start_sample']:
                raise ValueError('The snap override would move the next clip before its neighbor. Reduce the overlap.')
            resolution = {'mode': 'override', 'owner_layer_id': owner['id'], 'offset_samples': offsets[side]}
            if side in policy.get('source_pause_sides', {}).get(key, ()):
                resolution['offset_origin'] = 'source_pause'
            return start, resolution
    continuous = _continuous_context(prior, layer)
    fallback = None
    explicit = [(owner, side) for owner, side in owners
                if _saved_join_mode(owner, side) is not None]
    guide_owners = [(edge, side, edge if explicit or continuous else _context_edge_owner(edge, side, layers))
                    for edge, side in explicit or owners]
    if not explicit and not continuous:
        for _, _, owner in guide_owners:
            policy = owner.get('replacement_snap', {})
            if (policy.get('preserve_source_padding') and
                    choice_key(owner, policy) not in policy.get('modes', {})):
                # The chosen slice already accounts for the source pause.
                # Only a deliberately edited join may override that spacing.
                return prior['end_sample'], {'mode': 'source_padding', 'owner_layer_id': owner['id']}
    for edge, side, owner in guide_owners:
        policy = owner.get('replacement_snap', {})
        mode = (_saved_join_mode(owner, side) if explicit
                else policy.get('modes', {}).get(choice_key(owner, policy)))
        if continuous and not explicit and policy.get('apply_to_context_edges'):
            continue
        if mode is None:
            mode = policy.get('mode')
            if continuous and mode != 'edges':
                continue
        if mode is None:
            continue
        if fallback is None:
            fallback = {'mode': mode, 'owner_layer_id': owner['id'], 'fallback': 'clip_edges'}
        guarded_edge = _context_edge_owner(edge, side, layers) is not edge
        earliest = prior['start_sample']
        if guarded_edge:
            earliest = max(earliest, _quiet_overlap_start(prior, layer))
        candidates = []
        for end, start in FAMILIES[mode]:
            try:
                target, incoming = guide(prior, end), guide(layer, start)
            except ValueError:
                continue
            if target.get('timing_partial') or incoming.get('timing_partial'):
                continue
            position = prior['start_sample'] + target['sample'] - incoming['sample']
            if position >= earliest:
                candidates.append((position, end, start))
            elif guarded_edge and fallback['owner_layer_id'] == owner['id']:
                fallback['reason'] = 'outside_measured_quiet'
        if candidates:
            position, end, start = min(candidates, key=lambda item: abs(item[0] - prior['end_sample']))
            return position, {'mode': mode, 'owner_layer_id': owner['id'],
                              'target_guide_id': end, 'own_guide_id': start}
    if fallback is not None:
        return prior['end_sample'], fallback
    if continuous:
        return prior['end_sample'], {'mode': 'continuity'}
    return None


def references(layer):
    policy = layer.get('replacement_snap')
    if policy is None:
        return []
    if (not isinstance(policy, dict) or set(policy) - {
            'mode', 'context_choice', 'overrides', 'modes', 'join_modes', 'source_pause_sides',
            'apply_to_context_edges', 'preserve_source_padding'}
            or policy.get('mode') not in (None, *FAMILIES)
            or any(type(policy.get(field, False)) is not bool
                   for field in ('apply_to_context_edges', 'preserve_source_padding'))
            or 'context_choice' in policy and not isinstance(policy['context_choice'], str)):
        raise ValueError('Invalid replacement snap settings.')
    result = []
    for field in ('overrides', 'modes', 'join_modes'):
        settings = policy.get(field, {})
        if not isinstance(settings, dict) or len(settings) > 10000:
            raise ValueError('Invalid replacement snap settings.')
        for key, value in settings.items():
            if not isinstance(key, str) or not key.partition(':')[2] or not key.startswith(('asset:', 'choice:')):
                raise ValueError('Invalid replacement snap recording key.')
            if field == 'overrides':
                _offsets(value)
            elif field == 'join_modes':
                if (not isinstance(value, dict) or not value or set(value) - {'lead', 'tail'}
                        or any(not isinstance(mode, str) or mode not in FAMILIES for mode in value.values())):
                    raise ValueError('Invalid saved join guide family.')
            elif not isinstance(value, str) or value not in FAMILIES:
                raise ValueError('Invalid saved snap guide family.')
            if key.startswith('asset:'):
                result.append(key[6:])
    matches = policy.get('source_pause_sides', {})
    if not isinstance(matches, dict) or len(matches) > 10000:
        raise ValueError('Invalid source pause settings.')
    for key, sides in matches.items():
        offsets = policy.get('overrides', {}).get(key, {})
        if (not isinstance(key, str) or not key.startswith('choice:') or not key[7:]
                or not isinstance(sides, list) or not 1 <= len(sides) <= 2
                or any(side not in ('lead', 'tail') or side not in offsets for side in sides)
                or len(set(sides)) != len(sides)):
            raise ValueError('Invalid source pause settings.')
    return result


def remap(layer, ids):
    policy = layer.get('replacement_snap', {})
    for field in ('overrides', 'modes', 'join_modes'):
        if policy.get(field):
            policy[field] = {'asset:' + ids[key[6:]] if key.startswith('asset:') else key: value
                             for key, value in policy[field].items()}
