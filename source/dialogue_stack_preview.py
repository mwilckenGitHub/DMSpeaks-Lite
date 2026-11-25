"""Audition all saved contextual replacements together or in context."""
import array
import copy
import hashlib
import uuid

from audio import encode_preview_pcm
from comparison_context import audition_window_pcm, rendered_window
from content_review import assert_audio_usable
from contextual_choices import FORMAT, current_record_for, replace
from contextual_return import verify_saved_media
from engine_limits import MAX_AUDIO_SECONDS, MAX_SEGMENTS, silence_duration
from guide_bindings import parent_id
from layered_audio import LayerSourceCache, RATE, render_layers


def saved_occurrences(app, base):
    """Find saved alternatives by occurrence, even for repeated delimiters."""
    groups = base.get('contextual_choices') or {}
    assembly = base.get('contextual_assembly') or {}
    if (assembly.get('format') != FORMAT or not groups
            or base.get('processing_definition') is not None or base.get('replacement_playback')):
        raise ValueError('Open a dry dialogue arrangement with saved contextual alternatives.')
    current_record_for(app, base)
    assert_audio_usable(base, app.store, audition_only=True)
    original = {layer['id']: layer for layer in base['layers']}
    occurrences = {
        f"packet:{len(region)}:{region}:word": group for region, group in groups.items()
    }
    if not occurrences.keys() <= original.keys():
        raise ValueError('Restore the original contextual clips before building the preview.')
    return original, occurrences


def with_choice(app, base, root, ident, cache):
    changed = replace(app, base, {'layer_id': root, 'candidate_id': ident}, source_cache=cache)
    return changed[0] if changed else base['layers']


def stack_layout(app, base, source_cache, original, occurrences):
    """Return an audition graph with complete clips and one shared base track."""
    if len(original) + sum(len(group['choices']) - 1 for group in occurrences.values()) > MAX_SEGMENTS:
        raise ValueError('The saved alternatives exceed the current preview clip limit.')

    layers, ends, members = [], {}, {}
    for layer in base['layers']:
        root = layer['id']
        if root not in occurrences:
            layers.append(copy.deepcopy(layer))
            continue

        alternatives = []
        for index, (ident, choice) in enumerate(occurrences[root]['choices'].items()):
            source_layers = with_choice(app, base, root, ident, source_cache)
            candidate = copy.deepcopy(next(item for item in source_layers if item['id'] == root))
            candidate.update(id=root if index == 0 else f'{root}--replacement-{index}',
                             label=choice['value'], guide_binding=None, after_layer_id=None,
                             gap_samples=0, overlap_samples=0, anchor_start_sample=0)
            candidate.pop('replacement_snap', None)
            _, measurement = render_layers([candidate], base['tracks'], app.store,
                                           audition_only=True, source_cache=source_cache)
            length = measurement['layers'][0]['rendered_sample_count']
            alternatives.append((candidate, length))

        parent = parent_id(layer)
        for candidate, _ in alternatives:
            candidate['anchor_start_sample'] = layer['start_sample']
            if parent:
                candidate['guide_binding'] = {
                    'target_layer_id': parent, 'target_guide_id': 'clip_end', 'own_guide_id': 'clip_start',
                    'offset_samples': layer['start_sample'] - original[parent]['end_sample'],
                }
            layers.append(candidate)
        ends[root] = max(alternatives, key=lambda item: item[1])[0]['id']
        members[root] = [candidate['id'] for candidate, _ in alternatives]

    # Rewire direct followers after measuring every group. This also covers
    # a stack immediately followed by another stack.
    for layer in layers:
        parent = parent_id(layer)
        if parent in ends:
            layer.update(after_layer_id=ends[parent], guide_binding=None,
                         gap_samples=0, overlap_samples=0)
            layer.pop('replacement_snap', None)
    return layers, members


def stack(app, base, cache, original, occurrences, status):
    status('Measuring saved replacement clips…')
    layers, members = stack_layout(app, base, cache, original, occurrences)
    status('Building the stacked preview…')
    pcm, mix = render_layers(layers, base['tracks'], app.store, audition_only=True, source_cache=cache)
    by_id = {layer['id']: layer for layer in mix['layers']}
    stack_ends = []
    member_ids = set()
    for ids in members.values():
        alternatives = [by_id[ident] for ident in ids]
        if len({layer['start_sample'] for layer in alternatives}) != 1:
            raise ValueError('The replacement clips could not share a start.')
        member_ids.update(ids)
        stack_ends.append(max(layer['end_sample'] for layer in alternatives))

    tracks = {track['id']: track['name'] for track in mix['tracks']}
    events = [{
        'candidate_id': layer['track_id'] if layer['id'] in member_ids else None,
        'clip_role': layer.get('clip_role'),
        'label': layer.get('label') or layer.get('slot', {}).get('spoken_text') or tracks[layer['track_id']],
        'layer_id': layer['id'], 'track_id': layer['track_id'],
        'start_sample': layer['start_sample'], 'end_sample': layer['end_sample'],
    } for layer in mix['layers']]
    comparison = {'mode': 'dialogue_stack', 'events': events, 'track_names': tracks,
                  'stack_ends': stack_ends, 'master_gain': mix['master_gain'],
                  'occurrences': len(members), 'recordings': len(member_ids)}
    return pcm, mix['channels'], comparison


def sequence(app, base, cache, original, occurrences, data, status):
    before = silence_duration(data.get('before_ms', 3000), 'Before Replacement')
    after = silence_duration(data.get('after_ms', 1000), 'After Replacement')
    lead, tail = (round(value * RATE / 1000) for value in (before, after))
    gap_ms = silence_duration(data.get('comparison_gap_ms', 250), 'Pause between comparisons')
    gap = round(gap_ms * RATE / 1000)
    channels = base['channels']
    tracks = {track['id']: track['name'] for track in base['tracks']}
    pcm = array.array('f')
    chapters = []
    total = sum(len(group['choices']) for group in occurrences.values())
    ordered = sorted(occurrences, key=lambda root: (original[root]['start_sample'], original[root]['slot_order']))
    for root in ordered:
        for ident, choice in occurrences[root]['choices'].items():
            status(f'Preparing replacement {len(chapters)+1} of {total}…')
            layers = with_choice(app, base, root, ident, cache)
            rendered, mix = render_layers(layers, base['tracks'], app.store, audition_only=True, source_cache=cache)
            clip = next(layer for layer in mix['layers'] if layer['id'] == root)
            first, last = clip['start_sample'], clip['end_sample']
            lo, hi = rendered_window(first, last, mix['sample_count'], lead, tail)
            start = len(pcm) // channels + (gap if chapters else 0)
            if start + hi - lo > RATE * MAX_AUDIO_SECONDS:
                raise ValueError('Comparison exceeds the current 30-minute preview limit.')
            if mix['channels'] != channels:
                raise ValueError('The saved alternatives do not share the dialogue channel layout.')
            if chapters:
                pcm.extend(array.array('f', [0]) * (gap * channels))
            pcm.extend(audition_window_pcm(rendered, channels, (lo, hi), (first, last), RATE))
            chapters.append({
                'slot_order': clip['slot_order'], 'track_id': clip['track_id'], 'value': choice['value'],
                'label': choice.get('label', ''), 'start_sample': start,
                'replacement_start_sample': start + first - lo, 'end_sample': start + hi - lo,
                'before_samples': first - lo, 'after_samples': hi - last,
            })
    comparison = {'mode': 'dialogue_sequence', 'chapters': chapters, 'track_names': tracks,
                  'before_ms': before, 'after_ms': after, 'comparison_gap_ms': gap_ms,
                  'occurrences': len(occurrences), 'recordings': total}
    return pcm, channels, comparison


def render(app, data, status):
    if not isinstance(data.get('asset_id'), str):
        raise ValueError('Choose a dialogue arrangement to preview.')
    base = app.store.asset(data['asset_id'])
    original, occurrences = saved_occurrences(app, base)
    cache = LayerSourceCache()
    mode = data.get('mode', 'stacked')
    if mode == 'stacked':
        pcm, channels, comparison = stack(app, base, cache, original, occurrences, status)
    elif mode == 'sequence':
        pcm, channels, comparison = sequence(app, base, cache, original, occurrences, data, status)
    else:
        raise ValueError('Choose Stacked Replacements or One at a Time with Context.')
    sample_count = len(pcm) // channels
    ident = uuid.uuid4().hex
    path = app.folder / 'comparison-previews' / (ident + '.flac')
    try:
        encode_preview_pcm(pcm, path, channels=channels)
        with app.store.lock:
            record = current_record_for(app, base)
            assert_audio_usable(app.store.asset(base['id']), app.store, audition_only=True)
            for asset_id in {choice['asset_id'] for group in occurrences.values()
                             for choice in group['choices'].values()}:
                verify_saved_media(app, record, app.store.asset(asset_id))
            return app.store.add_asset({
                'id': ident, 'kind': 'comparison-preview', 'audition_only': True, 'path': str(path),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'seconds': sample_count / RATE, 'sample_count': sample_count,
                'sample_rate': RATE, 'channels': channels, 'words': [],
                'comparison': comparison,
            })
    except Exception:
        path.unlink(missing_ok=True)
        raise
