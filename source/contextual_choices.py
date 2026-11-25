"""Occurrence-specific saved returns and their atomic timeline edits."""
import copy

from contextual_layer_layout import _sha256_of_asset
from contextual_packet_layout import delimiter_track_id, packet_for_layer, packet_slices, returned_bounds
from engine_limits import MAX_SEGMENTS

FORMAT = 'dmspeaks-context-packets-1'
SILENCE_FALLBACK = 'silence-fallback'
SAVED_FALLBACK = 'saved-fallback'
PAGE_SIZE = 12


def _recording_label(choice):
    label = choice.get('label', '')
    if not isinstance(label, str) or len(label) > 80 or (label and not label.isprintable()):
        raise ValueError('Use a short, single-line contextual recording label.')
    return label.strip()


def _connected_join_sides(choice):
    """Read listening hints for the measured edges of this exact recording."""
    sides = choice.get('connected_join_sides', [])
    if (not isinstance(sides, list) or any(side not in ('lead', 'tail') for side in sides)
            or len(set(sides)) != len(sides)):
        raise ValueError('Connected speech hints must identify a lead or tail join.')
    return sides


def choice_matches(choice, query):
    return any(query in text.casefold() for text in (choice['value'], choice.get('label', '')))


def prepare(packets, assets, *, timed=False, fallback_by_region=None):
    """Bind alternatives to their occurrence and measured source windows."""
    groups = {}
    remaining = MAX_SEGMENTS
    for packet in packets:
        variants = packet.get('alternatives', [])
        if not isinstance(variants, list) or len(variants) > remaining:
            raise ValueError('Supply a bounded list of saved contextual alternatives.')
        remaining -= len(variants)
        region = packet['id']
        fallback = (fallback_by_region or {}).get(region)
        if fallback:
            variants = [*variants, fallback]
        if not variants:
            continue
        initial = {**packet, 'id': 'original', 'value': packet['slot'].get('value', '')}
        original = packet_slices(region, returned_bounds(packet['context_asset'],
                                 packet['returned_region'], packet['word_bounds']))
        choices = {}
        for variant in [initial, *variants]:
            if not isinstance(variant, dict):
                raise ValueError('Invalid saved contextual alternative.')
            ident, value, asset_id = (variant.get(key) for key in ('id', 'value', 'context_asset_id'))
            if not all(isinstance(item, str) and item.strip() for item in (ident, value, asset_id)) or ident in choices:
                raise ValueError('Each contextual choice needs a unique identity, value and saved return.')
            context = assets.get(asset_id)
            if context is None:
                raise ValueError('Choose an available saved return.')
            bounds = returned_bounds(context, variant.get('returned_region'), variant.get('word_bounds'))
            if original.keys() != packet_slices(region, bounds).keys():
                raise ValueError('Contextual choices must supply the same leading and trailing clip roles.')
            begin, wa, wb, end = bounds
            label = _recording_label(variant)
            connected = _connected_join_sides(variant)
            choices[ident] = dict(value=value, asset_id=asset_id, sha256=_sha256_of_asset(context),
                                  returned_region=[begin, end], word_bounds=[wa, wb],
                                  **({'label': label} if label else {}),
                                  **({'connected_join_sides': list(connected)} if connected else {}))
            if timed and variant.get('context_word_links'):
                choices[ident]['context_word_links'] = copy.deepcopy(variant['context_word_links'])
        groups[region] = dict(selected='original', choices=choices)
    return groups


def references(asset):
    """Return the media dependencies, including currently unselected returns."""
    groups = asset.get('contextual_choices', {})
    if not isinstance(groups, dict) or len(groups) > MAX_SEGMENTS:
        raise ValueError('Invalid contextual choice groups.')
    if groups and (asset.get('contextual_assembly') or {}).get('format') != FORMAT:
        raise ValueError('Contextual choices require a saved packet arrangement.')
    ids = []
    for region, group in groups.items():
        choices = group.get('choices') if isinstance(group, dict) else None
        if (not isinstance(region, str) or not region or not isinstance(choices, dict)
                or len(choices) < 2 or 'original' not in choices or not isinstance(group.get('selected'), str)
                or group['selected'] not in choices):
            raise ValueError('Invalid contextual choice selection.')
        for key, choice in choices.items():
            ident = choice.get('asset_id') if isinstance(choice, dict) else None
            if (not isinstance(key, str) or not key or not isinstance(ident, str) or not ident
                    or ident == asset.get('id') or not isinstance(choice.get('value'), str)
                    or not choice['value'].strip() or _sha256_of_asset(choice) is None):
                raise ValueError('Invalid saved contextual choice media.')
            _recording_label(choice)
            _connected_join_sides(choice)
            ids.append(ident)
            if len(ids) > 3 * MAX_SEGMENTS:
                raise ValueError('Too many saved contextual choices.')
    return ids


def validate_media(asset, assets):
    """Validate unopened choices as well as the arrangement's current media."""
    groups = asset.get('contextual_choices')
    if not groups:
        return
    provenance = asset['contextual_assembly'].get('packets')
    if not isinstance(provenance, list) or any(not isinstance(p, dict) for p in provenance):
        raise ValueError('Invalid contextual packet provenance.')
    for region, group in groups.items():
        packets = [p for p in provenance if p.get('region_id') == region]
        if len(packets) != 1:
            raise ValueError('A contextual choice needs one matching packet.')
        packet = packets[0]
        selected = group['choices'][group['selected']]
        if (packet.get('context_sha256') != selected['sha256'] or
                any(packet.get(key) != selected.get(key) for key in ('returned_region', 'word_bounds'))):
            raise ValueError('The selected contextual choice differs from the saved packet.')
        selected_slices = packet_slices(region, returned_bounds(assets[selected['asset_id']],
                                        selected.get('returned_region'), selected.get('word_bounds')))
        for choice in group['choices'].values():
            context = assets[choice['asset_id']]
            if (context.get('sha256') != choice['sha256'] or any(context.get(key) != asset.get(key)
                    for key in ('record_id', 'source_revision', 'source_metadata_revision'))):
                raise ValueError('Contextual choice media belongs to a different dialogue or revision.')
            bounds = returned_bounds(context, choice.get('returned_region'), choice.get('word_bounds'))
            if selected_slices.keys() != packet_slices(region, bounds).keys():
                raise ValueError('Contextual choices have inconsistent clip roles.')


def remap(asset, ids):
    for group in asset.get('contextual_choices', {}).values():
        for choice in group['choices'].values():
            choice['asset_id'] = ids[choice['asset_id']]
    assembly = asset.get('contextual_assembly')
    timing = assembly.get('timed_proposals') if isinstance(assembly, dict) else None
    held = timing.get('held') if isinstance(timing, dict) else None
    if isinstance(held, list):
        for item in held:
            if isinstance(item, dict) and isinstance(item.get('recording_id'), str):
                # A missing held return is still useful as a review note.
                item['recording_id'] = ids.get(item['recording_id'])


def current_record_for(app, base):
    dataset, index = base['record_id'].rsplit(':', 1)
    from clip_ownership import current_record
    record = current_record(app.store, dataset, int(index))
    if (base.get('source_revision') != record['revision'] or
            base.get('source_metadata_revision') != record.get('metadata_revision')):
        raise ValueError('The dialogue or speaker metadata changed. Review the saved returns again.')
    return record


def selection(app, base, layer_id):
    packet = packet_for_layer(base, layer_id)
    group = (base.get('contextual_choices') or {}).get(packet['region_id']) if packet else None
    if not group:
        raise ValueError('This occurrence has no saved contextual alternatives.')
    return packet, group, current_record_for(app, base)



def describe(app, base, data):
    packet, group, _ = selection(app, base, data.get('layer_id'))
    query, offset = data.get('query', ''), data.get('offset', 0)
    if not isinstance(query, str) or type(offset) is not int or offset < 0:
        raise ValueError('Choose a valid search and page offset.')
    query = query.casefold()
    candidates = [dict(id=ident, value=choice['value'], **({'label':choice['label']} if choice.get('label') else {}))
                  for ident, choice in group['choices'].items() if choice_matches(choice, query)]
    page = candidates[offset:offset + PAGE_SIZE]
    result = dict(asset_id=base['id'], layer_id=data['layer_id'], slot_order=packet['slot_order'],
                current_id=group['selected'], current_value=group['choices'][group['selected']]['value'],
                candidates=page, total=len(candidates),
                offset=offset, page_size=PAGE_SIZE)
    preferred_value = data.get('preferred_value')
    if preferred_value is not None:
        if not isinstance(preferred_value, str):
            raise ValueError('Choose a valid replacement value.')
        if not any(choice['value'] == preferred_value for choice in page):
            match = next((choice for choice in candidates if choice['value'] == preferred_value), None)
            if match:
                result['preferred_candidate'] = match
    return result


def replace(app, base, operation, *, source_cache=None):
    """Change only the selected packet's slices and preserve other clip edits."""
    layer_id = operation.get('layer_id')
    packet, group, record = selection(app, base, layer_id)
    ident = operation.get('candidate_id')
    if not isinstance(ident, str) or ident not in group['choices']:
        raise ValueError('Choose a contextual return saved for this occurrence.')
    if ident == group['selected']:
        return None
    old, new = (group['choices'][key] for key in (group['selected'], ident))
    old_asset, new_asset = (app.store.asset(choice['asset_id']) for choice in (old, new))
    from contextual_return import checked_assets
    checked_assets(app, record, list({item['id']: item for item in (old_asset, new_asset)}.values()),
                   source_cache=source_cache)
    if (old['sha256'] != old_asset['sha256'] or new['sha256'] != new_asset['sha256'] or
            any(packet[field] != old[field] for field in ('returned_region', 'word_bounds')) or
            packet['context_sha256'] != old['sha256']):
        raise ValueError('The saved contextual return changed. Reassemble it before switching.')
    region = packet['region_id']
    old_slices, new_slices = [packet_slices(region, returned_bounds(asset, choice['returned_region'],
                                                                  choice['word_bounds']))
                              for asset, choice in ((old_asset, old), (new_asset, new))]
    if old_slices.keys() != new_slices.keys():
        raise ValueError('The saved choices have different contextual clip roles.')
    layers = copy.deepcopy(base['layers'])
    by_id = {layer['id']: layer for layer in layers}
    previous = None
    for key, (lo, hi) in old_slices.items():
        layer = by_id.get(key, {})
        valid_tracks = ({'infill:' + region, delimiter_track_id(layer['slot']['type'])}
                        if key == layer_id and layer.get('slot', {}).get('type') else {'base'})
        if (layer.get('kind') != 'audio' or layer.get('asset_id') != old_asset['id']
                or (layer.get('source_in_sample'), layer.get('source_out_sample')) != (lo, hi)
                or layer.get('track_id') not in valid_tracks
                or any(layer.get(field) for field in ('section_history', 'source_mute_windows',
                        'rendered_region', 'processing_context', 'section_effects', 'group_section_effects'))
                or (previous and (layer.get('after_layer_id') != previous or layer.get('gap_samples')
                                  or layer.get('overlap_samples') or layer.get('guide_binding')))):
            raise ValueError('Restore the original contextual clip boundaries and links before switching.')
        previous = key
        layer['asset_id'] = new_asset['id']
        layer['source_in_sample'], layer['source_out_sample'] = new_slices[key]
        if key == layer_id:
            layer['label'] = new['value']
            spoken = '' if ident == SILENCE_FALLBACK and new_asset.get('kind') == 'marker-silence' else new['value']
            layer['slot'].update(value=new['value'], spoken_text=spoken, asset_id=new_asset['id'])
        else:
            if 'context_choice' in layer.get('replacement_snap', {}):
                layer['replacement_snap']['context_choice'] = ident
            if 'asset_id' in layer.get('slot', {}):
                layer['slot']['asset_id'] = new_asset['id']
            if new.get('context_word_links'):
                layer['context_word_links'] = copy.deepcopy(new['context_word_links'])
            else:
                layer.pop('context_word_links', None)
    provenance = copy.deepcopy(base['contextual_assembly'])
    from replacement_snap import select, select_for_delimiter
    if provenance.get('preserve_base') is True:
        # Measured cuts already own their padding. Keep authored recording
        # overrides, without applying the current timeline or dictionary mode.
        select(by_id[layer_id], {}, contextual_choice=ident)
    else:
        select_for_delimiter(by_id[layer_id], record, operation, contextual_choice=ident)
    selected = next(p for p in provenance['packets'] if p['region_id'] == region)
    selected.update(returned_region=new['returned_region'], word_bounds=new['word_bounds'],
                    context_sha256=new['sha256'])
    groups = copy.deepcopy(base['contextual_choices'])
    groups[region]['selected'] = ident
    return layers, provenance, groups
