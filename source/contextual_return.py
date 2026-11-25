"""Assemble an explicitly selected saved infill return without model calls.

This accepts measured source/edit/word coordinates. Locating and approving word
ownership is a separate step; word boundaries are not promoted to phoneme truth.
"""
import hashlib
import uuid
from array import array
from pathlib import Path

from audio import decode, encode_preview_pcm
from contextual_layer_layout import build_context_layout
from layered_audio import render_layers


def verify_saved_media(app, record, asset):
    """Check that a saved contextual asset still owns these exact bytes."""
    record_id = record['dataset'] + ':' + str(record['index'])
    from content_review import assert_audio_usable
    if asset.get('record_id') != record_id:
        raise ValueError('Choose saved source and return audio from this dialogue.')
    if (asset.get('source_revision') != record['revision'] or
            asset.get('source_metadata_revision') != record.get('metadata_revision')):
        raise ValueError('The source text or speaker metadata changed. Review the saved return.')
    assert_audio_usable(asset, app.store)
    with Path(asset['path']).open('rb') as media:
        actual = hashlib.file_digest(media, 'sha256').hexdigest()
    if actual != asset.get('sha256'):
        raise ValueError('Saved source or return media changed. No assembly was created.')


def checked_pcm(app, record, asset, *, source_cache=None):
    verify_saved_media(app, record, asset)
    pcm = None
    if source_cache is not None and asset.get('channels') == 1:
        samples, channels = source_cache.read(asset['path'])
        if channels == 1:
            pcm = samples
    if pcm is None:
        pcm = decode(asset['path'])
    if len(pcm) != asset.get('sample_count'):
        raise ValueError('Saved source or return length differs from its measured samples.')
    return pcm


def checked_assets(app, record, assets, *, source_cache=None):
    for asset in assets:
        checked_pcm(app, record, asset, source_cache=source_cache)


def _store_marker_silence(app, source, sample_count):
    """Store an empty replacement on the same sample clock as the literal marker."""
    ident = uuid.uuid4().hex
    path = app.audio.root / (ident + '.flac')
    encode_preview_pcm(array('f', [0.0]) * sample_count, path)
    identity = {key: source.get(key) for key in (
        'dataset', 'line_id', 'record_id', 'source_revision', 'source_metadata_revision')}
    return app.store.add_asset(dict(
        **identity, id=ident, kind='marker-silence', path=str(path),
        sample_rate=44100, sample_count=sample_count, channels=1,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest()))


def store_layout(app, data, record, layout, status):
    record_id = record['dataset'] + ':' + str(record['index'])
    status('Scheduling the saved base and selected infill…')
    pcm, rendered = render_layers(layout['layers'], layout['tracks'], app.store,
                                  normalize_master=not layout['provenance'].get('preserve_base', False))
    current = app.record(data)
    if (current['revision'] != record['revision'] or
            current.get('metadata_revision') != record.get('metadata_revision')):
        raise ValueError('The source changed during assembly. Review it and try again.')
    path = app.audio.root / (uuid.uuid4().hex + '.flac')
    encode_preview_pcm(pcm, path, channels=rendered['channels'])
    return app.store.add_asset({
        **rendered, 'kind': 'inspection-preview', 'audition_only': True,
        'dataset': record['dataset'],
        'line_id': record['id'], 'record_id': record_id,
        'source_revision': record['revision'],
        'source_metadata_revision': record.get('metadata_revision'),
        'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'contextual_assembly': layout['provenance'],
        **({'contextual_choices': layout['choices']} if layout.get('choices') else {}),
        'listening_review': 'pending',
        'timing_source': rendered.get('timing_source', 'explicit saved-return sample coordinates; word ownership unverified'),
    })


def assemble(app, data, status):
    record = app.record(data)
    source = app.store.asset(data['source_asset_id'])
    context = app.store.asset(data['context_asset_id'])
    checked_assets(app, record, [source, context])
    if data.get('generated_window') is None:
        raise ValueError('Supply the generated region from the saved return metadata.')
    layout = build_context_layout(source, context, data['source_window'], data['word_bounds'],
        generated_window=data['generated_window'], mode=data.get('mode', 'source'),
        slot=data.get('slot'), base_join=data.get('base_join'))
    return store_layout(app, data, record, layout, status)


def audition_measured_native_join(app, data, source, specs, assets, status):
    """Use the verified native join when one requested return has no packet window."""
    if len(specs) != 1:
        return None
    spec = specs[0]
    if (not spec.get('auto_context') or 'manual_base_cut' in spec
            or len(spec.get('alternatives', [])) != 1):
        return None
    choice = spec['alternatives'][0]
    if 'manual_return_cut' in choice:
        return None
    if spec.get('initial_choice_id') != choice['id']:
        return None
    context = assets[choice['context_asset_id']]
    receipt = context.get('native_context_take')
    piece_id = context.get('source_clip_id')
    if (not isinstance(receipt, dict) or not isinstance(piece_id, str) or
            context.get('source_take_asset_id') != source['id']):
        return None
    try:
        piece = app.store.asset(piece_id)
        if (piece.get('slot_order') != spec['slot_order'] or piece.get('value') != choice['value'] or
                piece.get('source_take_asset_id') != source['id'] or
                receipt.get('insert_sha256') != piece.get('sha256')):
            return None
        verify_saved_media(app, app.record(data), context)
        from native_edit_workflow import inspect_measured_join
        status('Checking the measured join from the saved Fish return…')
        return inspect_measured_join(app, {**data, 'asset_id': piece['id'], 'suggested_join': True,
                                         'match_padding': spec.get('match_padding', False),
                                         'min_dbfs': spec.get('min_dbfs', -45)}, status)
    except (ValueError, FileNotFoundError):
        return None  # Preserve the planner's hold when the measured join is also unavailable.


def assemble_packets(app, data, status):
    from contextual_choices import FORMAT, SAVED_FALLBACK, SILENCE_FALLBACK, current_record_for, prepare
    from contextual_packet_layout import build_packet_layout
    from delimiter_definitions import registered
    from engine_limits import MAX_SEGMENTS
    from replacement_snap import carry_recording_settings, requested_mode, select_for_delimiter
    record = app.record(data)
    source = app.store.asset(data['source_asset_id'])
    timed = 'timed_packets' in data
    preserve_base = data.get('preserve_base', False)
    if type(preserve_base) is not bool or preserve_base and not timed:
        raise ValueError('Preserving the base is available for timed returns.')
    snap_mode = requested_mode(data)
    review_partial = data.get('review_partial', False)
    if type(review_partial) is not bool or review_partial and not timed:
        raise ValueError('Partial review is available for timed returns.')
    if timed and 'packets' in data:
        raise ValueError('Choose explicit coordinates or timed packets for this preview.')
    previous = None
    if 'rebuild_from_asset_id' in data:
        ident = data['rebuild_from_asset_id']
        if not timed or not isinstance(ident, str) or not ident:
            raise ValueError('Choose a timed preview to rebuild.')
        previous = app.store.asset(ident)
        if ((previous.get('contextual_assembly') or {}).get('format') != FORMAT
                or not isinstance(previous['contextual_assembly'].get('timed_proposals'), dict)
                or previous.get('record_id') != source.get('record_id')
                or previous.get('replacement_playback') or previous.get('processing_definition') is not None):
            raise ValueError('Choose the saved timed preview for this dialogue.')
        current_record_for(app, previous)
    raw = data.get('timed_packets' if timed else 'packets')
    if not isinstance(raw, list) or len(raw) > MAX_SEGMENTS or any(not isinstance(p, dict) for p in raw):
        raise ValueError('Supply a bounded list of returned context regions.')
    if timed and not raw:
        raise ValueError('Choose at least one timed delimiter occurrence.')
    assets = {}
    definitions = {item['key']: item for item in registered(record)}
    preferred_choices = {}
    alternative_count = 0
    for packet in raw:
        alternatives = packet.get('alternatives', [])
        if not isinstance(alternatives, list) or len(alternatives) > MAX_SEGMENTS - alternative_count:
            raise ValueError('Supply a bounded list of saved contextual alternatives.')
        alternative_count += len(alternatives)
        if timed and 'initial_choice_id' in packet:
            preferred = packet['initial_choice_id']
            order = packet.get('slot_order')
            if (type(order) is not int or not isinstance(preferred, str) or not preferred or
                    not any(isinstance(choice, dict) and choice.get('id') == preferred
                            for choice in alternatives)):
                raise ValueError('Choose an initial recording from the submitted alternatives.')
            preferred_choices[order] = preferred
        for entry in alternatives if timed else [packet, *alternatives]:
            ident = entry.get('context_asset_id') if isinstance(entry, dict) else None
            if not isinstance(ident, str) or not ident:
                raise ValueError('Choose an available saved return.')
            if ident not in assets:
                assets[ident] = app.store.asset(ident)
    if timed:
        from native_context_take import verified_cut_region
        from timed_contextual_packets import plan_packets, saved_fallbacks
        status('Checking phoneme alignment, word times and quiet joins…')
        assets[source['id']] = source
        packets, timing_evidence = plan_packets(record, source, raw, assets,
                                               lambda asset: checked_pcm(app, record, asset),
                                               partial=review_partial, preserve_base=preserve_base,
                                               verify_native_cut=lambda context, pcm: verified_cut_region(
                                                   app, record, source, context, pcm),
                                               align_phones=(lambda asset, transcript: app.align_phonemes(
                                                   record, asset, transcript)) if hasattr(app, 'align_phonemes') else None,
                                               find_fallback=lambda slot, value, excluded: saved_fallbacks(
                                                   app.store, source, slot, value, excluded),
                                               fallback_values={key: item.get('default')
                                                                for key, item in definitions.items()})
        if review_partial and not packets:
            if any(item.get('code') == 'phoneme_join_rejected'
                   for item in timing_evidence['held']):
                return dict(asset=None, held=timing_evidence['held'], ready_count=0)
            native = audition_measured_native_join(app, data, source, raw, assets, status)
            if native:
                return dict(asset=native, held=[], ready_count=1)
            return dict(asset=None, held=timing_evidence['held'], ready_count=0)
    else:
        packets = [{**p, 'context_asset': assets[p['context_asset_id']]} for p in raw]
        checked_assets(app, record, [source, *assets.values()])
    fallback_by_region = {}
    eligible_orders = {item['slot_order'] for item in timing_evidence['packets']
                       if item.get('literal_silence_eligible')} if timed else set()
    for packet in packets:
        slot = packet['slot']
        if not (preserve_base and slot.get('placeholder') and slot['value'] == slot['placeholder']):
            continue
        order = slot['order']
        alternatives = packet['alternatives']
        if any(take['id'] == preferred_choices.get(order) for take in alternatives):
            continue
        fallback = definitions.get(slot['type'], {}).get('default')
        matches = [take['id'] for take in alternatives if take['value'] == fallback]
        if len(matches) == 1:
            preferred_choices[order] = matches[0]
        elif 'saved_fallback' in packet:
            fallback_by_region[packet['id']] = packet['saved_fallback']
            preferred_choices[order] = SAVED_FALLBACK
        elif order in eligible_orders:
            start, end = packet['source_region']
            silence = _store_marker_silence(app, source, end - start)
            assets[silence['id']] = silence
            bounds = [0, end - start]
            fallback_by_region[packet['id']] = dict(
                id=SILENCE_FALLBACK, value='Silence', context_asset_id=silence['id'],
                returned_region=bounds, word_bounds=bounds)
            preferred_choices[order] = SILENCE_FALLBACK
    choices = prepare(packets, assets, timed=timed, fallback_by_region=fallback_by_region)
    context_links = {}
    selected_packets = []
    for packet in packets:
        group = choices.get(packet['id'])
        slot = packet['slot']
        preferred = preferred_choices.get(slot['order'])
        choice = group['choices'].get(preferred) if group and preferred else None
        if choice:
            group['selected'] = preferred
            ident, value = choice['asset_id'], choice['value']
            spoken = '' if preferred == SILENCE_FALLBACK and packet['id'] in fallback_by_region else value
            selected_packets.append({**packet, 'context_asset': assets[ident],
                'returned_region': choice['returned_region'], 'word_bounds': choice['word_bounds'],
                'slot': {**slot, 'value': value, 'spoken_text': spoken, 'asset_id': ident}})
            context_links[packet['id']] = choice.get('context_word_links')
        else:
            selected_packets.append(packet)
    layout = build_packet_layout(source, selected_packets, context_links=context_links)
    if preserve_base:
        layout['provenance']['preserve_base'] = True
    selected_by_order = {packet['slot_order']: choices[packet['region_id']]['selected']
                         for packet in layout['provenance']['packets'] if packet['region_id'] in choices}
    matched_orders = {setting['slot_order'] for setting in timing_evidence['settings']
                      if setting['match_padding']} if timed else set()
    for layer in layout['layers']:
        if preserve_base or layer.get('slot', {}).get('type') == 'fixed':
            continue
        select_for_delimiter(layer, record, data, definitions=definitions,
                             contextual_choice=selected_by_order.get(layer['slot_order']))
        if timed:
            policy = layer.setdefault('replacement_snap', {})
            policy['apply_to_context_edges'] = True
            if layer['slot_order'] in matched_orders:
                policy['preserve_source_padding'] = True
    if timed:
        if 'snap_mode' in data:
            timing_evidence['snap_mode'] = snap_mode
        layout['provenance']['timed_proposals'] = timing_evidence
    layout['choices'] = choices
    if previous is not None:
        review = carry_recording_settings(previous, layout)
        if review:
            timing_evidence['snap_edits_to_review'] = review
    result = store_layout(app, data, record, layout, status)
    if review_partial:
        return dict(asset=result, held=timing_evidence.get('held', []),
                    ready_count=sum(len(packet['alternatives']) for packet in packets))
    return result
