"""Select an occurrence's compatible recording without rebuilding its arrangement."""
import copy
from pathlib import Path
from engine_limits import silence_duration
from audio import decode, probe
from model import segment_audio_matches
from content_review import is_rejected, is_delivery_eligible, assert_audio_usable

PAGE_SIZE = 12


def context(app, data):
    from replacement_playback import source_asset
    base = source_asset(app.store, app.store.asset(data['asset_id']))
    from contextual_choices import FORMAT
    if (base.get('contextual_assembly') or {}).get('format') == FORMAT:
        raise ValueError('Choose a saved contextual return to change this occurrence and its neighboring speech together.')
    ident = data.get('layer_id')
    layers = base.get('layers', [])
    layer = next((l for l in layers if l['id'] == ident), None)
    if not layer or layer.get('slot', {}).get('type') in (None, 'fixed'):
        raise ValueError('Choose a replacement clip.')
    # A section edit is deliberate; replacing the entire occurrence would erase it.
    if sum(l.get('slot_order') == layer.get('slot_order') and l.get('slot', {}).get('type') not in (None, 'fixed') for l in layers) != 1:
        raise ValueError('This replacement has multiple edited sections. Use an unsplit occurrence to select another candidate.')
    dataset, index = base['record_id'].rsplit(':', 1)
    from clip_ownership import current_record
    record = current_record(app.store, dataset, int(index))
    if base.get('source_revision') != record['revision'] or base.get('source_metadata_revision') != record.get('metadata_revision'):
        raise ValueError('The corpus changed after this arrangement. Reassemble it before choosing replacements.')
    origin = layer.get('replacement_origin_asset_id') or layer.get('asset_id')
    exemplar = app.store.asset(origin) if origin and (layer.get('kind') != 'silence' or layer.get('replacement_origin_asset_id')) else None
    inspection = base.get('kind') == 'inspection-preview' and base.get('audition_only') is True
    key = exemplar.get('generation_key') if exemplar else app.generation_key(record, data.get('performance', data))
    choices = app.audio.candidate_query(record,{**layer['slot'],'order':layer['slot_order']},
        (exemplar or {}).get('variant',data.get('variant','default')),generation_key=key,
        track=True,inspection=inspection,any_value=True,require_path=True)
    return base, layer, record, exemplar, inspection, choices


def describe(app, data):
    from contextual_choices import FORMAT, describe as describe_contextual
    base = app.store.asset(data['asset_id'])
    if (base.get('contextual_assembly') or {}).get('format') == FORMAT:
        return describe_contextual(app, base, data)
    base, layer, record, exemplar, inspection, choices = context(app, data)
    query = data.get('query', '')
    offset = data.get('offset', 0)
    if not isinstance(query, str) or type(offset) is not int or offset < 0:
        raise ValueError('Choose a valid search and page offset.')
    preferred_value = data.get('preferred_value')
    if preferred_value is not None and not isinstance(preferred_value, str):
        raise ValueError('Choose a valid replacement value.')
    page=choices.page(query,offset,PAGE_SIZE,preferred_value=preferred_value)
    return {'asset_id': base['id'], 'layer_id': layer['id'], 'slot_order': layer['slot_order'],
            'placeholder': layer['slot'].get('placeholder', ''), 'inspection_only': inspection,
            'current_id': layer.get('asset_id') if layer['kind'] == 'audio' else 'silence',
            'current_label': layer['slot'].get('value') or layer.get('label') or 'Current recording',
            'candidates': [{'id': c['id'], 'value': c.get('value', ''), 'seconds': c.get('seconds', 0),
                            'review_pending': c['review_pending']} for c in page['candidates']],
            'offset': offset, 'total': page['total'], 'page_size': PAGE_SIZE,
            **({'preferred_candidate': page['preferred_candidate']} if 'preferred_candidate' in page else {})}


def replace(app, base, operation):
    _, layer, record, exemplar, inspection, choices = context(app, {**operation, 'asset_id': base['id']})
    candidate_id = operation.get('candidate_id')
    layers = copy.deepcopy(base['layers'])
    selected = next(l for l in layers if l['id'] == layer['id'])
    if exemplar:
        selected['replacement_origin_asset_id'] = exemplar['id']
    if candidate_id == 'silence':
        ms = silence_duration(operation.get('silence_ms', 0))
        count = round(ms * 44100 / 1000)
        selected.update(kind='silence', asset_id=None, label='Skip' if ms == 0 else f'Silence · {ms} ms', effects=[])
        selected['group_section_effects']=[]
        selected['slot'].update(kind='silence', asset_id=None, value='', spoken_text='')
    else:
        candidate = choices.find(candidate_id)
        if candidate is None:
            raise ValueError('This recording does not match this occurrence, voice, source revision or performance.')
        assert_audio_usable(candidate, app.store, audition_only=inspection)
        channels = probe(candidate['path'])['channels']
        count = len(decode(candidate['path'], channels=channels)) // channels
        selected.update(kind='audio', asset_id=candidate['id'], label=str(candidate.get('value', '')))
        selected['slot'].update(kind='audio', asset_id=candidate['id'], value=candidate.get('value', ''), spoken_text=candidate.get('insert_expected_text') or candidate.get('value', ''))
        from clip_groups import describe as describe_groups
        from clip_group_schema import effective_effects,effective_section_effects
        from performance_plan import parse_record
        segments = parse_record(record, base.get('player', {}))['segments']
        group = next((g for g in describe_groups(app.store, record, segments)['groups'] if g['slot_order'] == layer['slot_order']), None)
        selected['effects'] = effective_effects(group, candidate['id']) if group else []
        selected['group_section_effects'] = effective_section_effects(group, candidate['id']) if group else []
        if group:selected['group_section_effects_id'] = group['id']
    # Start dependency, track controls, gain and pace stay intact. Full new source
    # replaces the old source trim/mutes; following dependencies use its actual end.
    selected.update(original_sample_count=count, original_sample_rate=44100,
                    original_seconds=count/44100, source_in_sample=0, source_out_sample=count)
    selected.pop('source_mute_windows', None)
    selected.pop('source_mute_clock_rate', None)
    selected.pop('processing_context', None)
    selected.pop('rendered_region', None)
    selected.pop('rendered_envelopes', None)
    selected.pop('section_history', None)
    from replacement_snap import select_for_delimiter
    select_for_delimiter(selected, record, operation)
    return layers, copy.deepcopy(base['tracks'])
