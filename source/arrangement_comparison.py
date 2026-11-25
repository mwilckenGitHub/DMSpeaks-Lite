"""Audition replacement choices through the same edited-track renderer as delivery.

No arrangement is saved or changed. Candidate source trims reset as they do when
choosing a new recording. Combined auditions sequence the complete processed
clips, while repeated-context auditions retain each choice's joins.
"""
from engine_limits import MAX_AUDIO_SECONDS, silence_duration
import array
import copy
import hashlib
import uuid

from audio import encode_preview_pcm
from content_review import media_digest
import contextual_choices
from comparison_context import audition_window_pcm, context_bounds, rendered_window
from layered_audio import LayerSourceCache, RATE, render_layers
from track_candidates import context, replace
from guide_bindings import binding


class ContextualCatalog:
    """A bounded occurrence catalog with IDs distinct from its media IDs."""

    contextual = True

    def __init__(self, app, group):
        self.selected = group['selected']
        self.items = {}
        for ident, choice in group['choices'].items():
            asset = app.store.asset(choice['asset_id'])
            digest = media_digest(asset)
            if digest != choice['sha256']:
                raise ValueError('A saved contextual recording changed. Reassemble it before comparing.')
            start, end = choice['word_bounds']
            self.items[ident] = {'id': ident, 'value': choice['value'], 'seconds': (end-start)/RATE,
                                 'sha256': digest, 'word_bounds': [start,end],
                                 'returned_region': choice['returned_region'],
                                 **({'label': choice['label']} if choice.get('label') else {})}

    def page(self, query, offset, limit, *, with_digest=False):
        if not isinstance(query, str):
            raise ValueError('Enter a search string.')
        folded = query.casefold()
        rows = [item for item in self.items.values() if contextual_choices.choice_matches(item, folded)]
        return {'candidates': rows[offset:offset+limit], 'total': len(rows)}

    def find(self, ident):
        return self.items.get(ident)

    def signature(self):
        return {'selected': self.selected, 'items': list(self.items.values())}


def selection_context(app, data):
    saved = app.store.asset(data['arrangement_id'])
    if (saved.get('contextual_assembly') or {}).get('format') == contextual_choices.FORMAT:
        if saved.get('processing_definition') is not None:
            raise ValueError('Open the dry arrangement before comparing contextual recordings.')
        layer_id = data.get('layer_id')
        packet, group, record = contextual_choices.selection(app, saved, layer_id)
        layer = next((item for item in saved['layers'] if item['id']==layer_id), None)
        if layer is None or sum(
                item.get('slot_order') == packet['slot_order'] and item.get('slot', {}).get('type') != 'fixed'
                for item in saved['layers']) != 1:
            raise ValueError('Restore the original contextual clip before comparing whole recordings.')
        result = saved, layer, record, None, saved.get('audition_only') is True, ContextualCatalog(app, group)
    else:
        result = context(app, {**data, 'asset_id': data['arrangement_id']})
    base, layer, record, _, _, choices = result
    if (record['dataset'] != data.get('dataset') or record['index'] != data.get('index')
            or layer['slot_order'] != data.get('slot_order')):
        raise ValueError('The selected arrangement belongs to another dialogue or occurrence.')
    if not getattr(choices, 'contextual', False):
        from clip_groups import arrangement_groups_current
        if not arrangement_groups_current(app, record, base):
            raise ValueError('Replacement group settings changed. Rebuild the affected group before comparing.')
    return result


def render(app, data, preview, target, checked, mode, silence_ms, gap_ms, fingerprint, status):
    base, selected, _, _, inspection, catalog = selection_context(app, data)
    contextual = getattr(catalog, 'contextual', False)
    scope = data.get('scope', 'nearby')
    if scope == 'join':
        before_ms = silence_duration(data.get('before_ms', 1000), 'Before Replacement')
        after_ms = silence_duration(data.get('after_ms', 1000), 'After Replacement')
        lead_frames = round(before_ms * RATE / 1000)
        tail_frames = round(after_ms * RATE / 1000)
    else:
        segments = preview['segments']
        left, right = context_bounds(segments, target['order'], data.get('before_words', 5),
                                    data.get('after_words', 5), scope)
        orders = {s['order'] for s in segments[left:right+1]}
    tracks = copy.deepcopy(base['tracks'])
    status('Auditioning replacements in the current tracks…')
    # Reuse the exact replacement operation, including per-candidate group FX.
    # The existing choice keeps its intentional source trim and individual edits.
    candidates, matching_contexts = [], {}
    source_cache = LayerSourceCache()
    snap_settings = {'snap_mode': data['snap_mode']} if 'snap_mode' in data else {}
    for ident in checked:
        operation = {'layer_id': selected['id'], 'candidate_id': ident, **snap_settings}
        if contextual:
            changed = contextual_choices.replace(app, base, operation, source_cache=source_cache)
            source_layers = changed[0] if changed else base['layers']
            matching_contexts[ident] = source_layers
            candidate = copy.deepcopy(next(layer for layer in source_layers if layer['id'] == selected['id']))
        elif ident == selected.get('asset_id') and selected['kind'] == 'audio':
            candidate = copy.deepcopy(selected)
        else:
            changed, _ = replace(app, base, {**operation, 'silence_ms': silence_ms,
                                             'performance': data})
            candidate = next(l for l in changed if l['id'] == selected['id'])
        candidate['comparison_candidate_id'] = ident
        candidates.append(candidate)

    if scope == 'clips' and all(c['kind'] == 'silence' and not c.get('original_sample_count') for c in candidates):
        return {'no_audio': True, 'notice': 'All checked alternatives were skipped.'}

    def build(alternatives, parallel=False, context_layers=None):
        alternatives = copy.deepcopy(alternatives)
        combined = len(alternatives) > 1
        if combined:
            # Use the active arrangement's start for the whole audition stack.
            # Joins authored for an individual recording may overlap a stack.
            for index, candidate in enumerate(alternatives):
                candidate.update(id='audition-' + str(index),guide_binding=None,after_layer_id=None,
                                 anchor_start_sample=selected['start_sample'],
                                 gap_samples=0,overlap_samples=0)
                candidate.pop('replacement_snap',None)
        if scope == 'clips':
            for candidate in alternatives:candidate.update(guide_binding=None,after_layer_id=None,anchor_start_sample=0,gap_samples=0,overlap_samples=0)
        if parallel and combined:
            # Measure processed durations, including tempo and effect tails,
            # with the same renderer. Followers attach to the longest result.
            for candidate in alternatives:
                if candidate['kind'] == 'silence' and not candidate.get('original_sample_count'):
                    candidate['rendered_sample_count'] = 0
                    continue
                sample = {**candidate, 'guide_binding': None, 'after_layer_id': None, 'anchor_start_sample': 0,
                          'gap_samples': 0, 'overlap_samples': 0}
                _, measured = render_layers([sample], tracks, app.store, audition_only=inspection,
                                            source_cache=source_cache)
                candidate['rendered_sample_count'] = measured['layers'][0]['rendered_sample_count']
            longest = max(alternatives, key=lambda candidate: candidate['rendered_sample_count'])
            follower = longest['id']
        else:
            for prior, candidate in zip(alternatives, alternatives[1:]):
                candidate.update(guide_binding=None,after_layer_id=prior['id'], gap_samples=0, overlap_samples=0)
            follower = alternatives[-1]['id']
        layers = []
        for original in context_layers if context_layers is not None else base['layers']:
            if original['id'] == selected['id']:
                layers.extend(alternatives)
            else:
                layer = copy.deepcopy(original)
                link = binding(layer)
                follows_selected = layer.get('after_layer_id') == selected['id']
                links_to_selected = link and link['target_layer_id'] == selected['id']
                if combined and (follows_selected or links_to_selected):
                    layer.update(guide_binding=None, after_layer_id=follower, gap_samples=0, overlap_samples=0)
                    layer.pop('replacement_snap', None)
                elif follows_selected:
                    layer['after_layer_id'] = follower
                elif links_to_selected:
                    link['target_layer_id'] = follower
                layers.append(layer)
        if scope == 'clips':
            # Clips-only excludes background/context while retaining track
            # channel, solo/mute, level and processing settings.
            layers = alternatives
            for candidate in layers:
                parent=candidate['guide_binding']['target_layer_id'] if binding(candidate) else candidate.get('after_layer_id')
                if parent not in {c['id'] for c in layers}:
                    candidate.update(guide_binding=None,after_layer_id=None, anchor_start_sample=0, gap_samples=0,
                                     overlap_samples=0)
        if scope == 'clips' and all(l['kind'] == 'silence' and not l.get('original_sample_count') for l in layers):
            return {'pcm': array.array('f'), 'channels': 1, 'events': [], 'master_gain': 1}
        pcm, info = render_layers(layers, tracks, app.store, audition_only=inspection,
                                  source_cache=source_cache)
        window = None
        if scope == 'dialogue':lo, hi = 0, info['sample_count']
        elif scope == 'join':
            # Window the actual mix so snaps, overlaps and track effects are
            # heard on their rendered clock, even without word timestamps.
            ids = {candidate['id'] for candidate in alternatives}
            replacements = [layer for layer in info['layers'] if layer['id'] in ids]
            first = min(layer['start_sample'] for layer in replacements)
            last = max(layer['end_sample'] for layer in replacements)
            lo, hi = rendered_window(first, last, info['sample_count'], lead_frames, tail_frames)
            window = {'before_samples': first - lo, 'after_samples': hi - last}
        else:
            included = [l for l in info['layers'] if l.get('slot_order') in orders]
            lo = min((l['start_sample'] for l in included), default=0)
            hi = max((l['end_sample'] for l in included), default=0)
        channels = info['channels']
        events = []
        for layer in info['layers']:
            if layer['end_sample'] < lo or layer['start_sample'] > hi:continue
            # Nearby context retains music on its actual timeline. Event source
            # and timeline bounds explicitly describe any window intersection.
            begin, end = max(lo, layer['start_sample']), min(hi, layer['end_sample'])
            events.append({'asset_id': layer.get('asset_id'), 'kind': layer['kind'],
                'slot_order': layer['slot_order'], 'layer_id': layer['id'],
                'track_id': layer['track_id'], 'clip_role': layer.get('clip_role'),
                'label': layer.get('label') or layer.get('slot', {}).get('spoken_text') or 'Clip',
                'muted': bool(layer.get('muted')),
                'candidate_id': layer.get('comparison_candidate_id'),
                'start_sample': begin-lo, 'end_sample': end-lo, 'sample_count': end-begin,
                'source_in_sample': layer['source_in_sample'],
                'source_out_sample': layer['source_out_sample'],
                'window_offset_sample': begin-layer['start_sample'],
                'effect_tail_sample_count': layer.get('effect_tail_sample_count', 0),
                'track_effect_tail_sample_count': layer.get('track_effect_tail_sample_count', 0)})
        audio = (audition_window_pcm(pcm, channels, (lo, hi), (first, last), RATE)
                 if window is not None else pcm[lo * channels:hi * channels])
        return {'pcm': audio, 'channels': channels,
                'events': events, 'master_gain': info['master_gain'], 'window': window}

    if mode == 'context' or len(candidates) == 1:
        auditions = (build([candidate], context_layers=matching_contexts.get(ident))
                     for candidate, ident in zip(candidates, checked))
    else:
        auditions = (build(candidates, parallel=mode == 'together'),)
    gap = round(gap_ms * RATE / 1000)
    total, outputs = 0, []
    for output in auditions:
        total += len(output['pcm']) // output['channels'] + (gap if outputs else 0)
        if total > RATE * MAX_AUDIO_SECONDS:
            raise ValueError('Comparison exceeds the current 30-minute preview limit.')
        outputs.append(output)
    channels = max(output['channels'] for output in outputs)
    pcm = array.array('f');events = [];windows = [];cursor = 0
    for iteration, output in enumerate(outputs):
        if iteration:
            pcm.extend(array.array('f', [0]) * (gap*channels));cursor += gap
        if output.get('window') is not None:
            windows.append({**output['window'], 'iteration': iteration, 'start_sample': cursor})
        for event in output['events']:
            events.append({**event, 'iteration': iteration,
                'start_sample': event['start_sample']+cursor,
                'end_sample': event['end_sample']+cursor})
        chunk = output['pcm']
        if output['channels'] != channels:
            chunk = array.array('f', (value for sample in chunk for value in (sample, sample)))
        pcm.extend(chunk);cursor += len(chunk)//channels
    if not cursor:return {'no_audio': True, 'notice': 'All checked alternatives were skipped.'}
    if cursor > RATE*MAX_AUDIO_SECONDS:raise ValueError('Comparison exceeds the current 30-minute preview limit.')
    from clip_comparison import _choices
    if _choices(app, data)[3] != fingerprint:
        raise ValueError('The source, track settings or audio changed during comparison. Reopen the audition.')
    ident = uuid.uuid4().hex;path = app.folder/'comparison-previews'/(ident+'.flac')
    try:
        encode_preview_pcm(pcm, path, channels=channels)
        return app.store.add_asset({'id': ident, 'kind': 'comparison-preview', 'path': str(path),
            'seconds': cursor/RATE, 'sample_count': cursor, 'sample_rate': RATE, 'channels': channels,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'words': [],
            'timing_source': 'edited-track sample clock, not word alignment',
            'comparison': {'mode': mode, 'scope': data.get('scope', 'nearby'),
                'slot_order': target['order'], 'arrangement_id': base['id'], 'events': events,
                'fingerprint': fingerprint, 'candidate_ids': checked,
                **({'before_ms': before_ms, 'after_ms': after_ms, 'context_windows': windows,
                    'comparison_gap_ms': gap_ms if mode == 'context' else 0}
                   if scope == 'join' else {}),
                'master_gain': min(output['master_gain'] for output in outputs),
                'whole_clips_preserved': scope != 'join', 'delivery_selection_unchanged': True,
                'edited_tracks_preserved': True},
            'notice': 'Audition only. Current tracks and delivery choices were not changed.'})
    except Exception:
        path.unlink(missing_ok=True)
        raise
