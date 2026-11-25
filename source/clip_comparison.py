"""Audition alternatives for one delimiter occurrence without changing delivery choices."""
from engine_limits import silence_duration, MAX_AUDIO_SECONDS
import array
import copy
import hashlib
import json
import uuid
from pathlib import Path

from audio import decode, probe, encode_preview_pcm
from comparison_context import context_bounds
from content_review import assert_audio_usable, media_digest

RATE = 44100
MAX_CHOICES = 32
PAGE_SIZE = 50


def _integer(value, label, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(label + ' must be an integer from 0 to ' + str(maximum) + '.')
    return value


def _choices(app, data):
    preview = app.preview(data);record = preview['record']
    if data.get('source_revision') not in (None, record['revision']):
        raise ValueError('The source changed. Reopen this delimiter group.')
    if data.get('source_metadata_revision') not in (None, record.get('metadata_revision')):
        raise ValueError('The speaker metadata changed. Reopen this delimiter group.')
    order = data.get('slot_order')
    matches = [s for s in preview['segments'] if s['order'] == order]
    if type(order) is not int or len(matches) != 1 or matches[0]['type'] == 'fixed':
        raise ValueError('Select one replacement delimiter occurrence.')
    target = matches[0]
    if target.get('speech_excluded'):raise ValueError('This delimiter is inside an unspoken cue.')
    key = app.generation_key(record, data)
    arrangement = None
    if data.get('arrangement_id'):
        from arrangement_comparison import selection_context
        arrangement, _, _, _, _, candidates = selection_context(app, data)
    else:
        candidates=app.audio.candidate_query(record,target,data.get('variant','default'),data.get('reference_id',''),key,any_value=True,require_path=True)
    material = {'dataset': record['dataset'], 'index': record['index'],
                'revision': record['revision'], 'metadata_revision': record.get('metadata_revision'),
                'slot_order': order, 'generation_key': key, 'catalog': candidates.signature(),
                'normal_options': [[s['order'], [a['id'] for a in s['options']]] for s in preview['segments']],
                'player': data.get('player', {}), 'selections': data.get('selections', {}),
                'silence_ms': data.get('silence_ms', 200), 'snap_mode': data.get('snap_mode'),
                'replacement_groups':preview.get('replacement_groups')}
    if arrangement is not None:
        material['arrangement'] = {'id': arrangement['id'], 'layers': arrangement['layers'], 'tracks': arrangement['tracks'],
            'sources': {l['asset_id']: media_digest(app.store.asset(l['asset_id'])) for l in arrangement['layers'] if l.get('asset_id')}}
    fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return preview, target, candidates, fingerprint


def describe(app, data):
    preview, target, choices, fingerprint = _choices(app, data)
    offset = _integer(data.get('offset', 0), 'Page offset', 10_000_000)
    query=data.get('query','');page=choices.page(query,offset,PAGE_SIZE,with_digest=True)
    record = preview['record']
    contextual = getattr(choices, 'contextual', False)
    return {'fingerprint': fingerprint, 'contextual': contextual, 'source_revision': record['revision'],
            **({'selected_id': choices.selected} if contextual else {}),
            'source_metadata_revision': record.get('metadata_revision'),
            'slot_order': target['order'], 'placeholder': target['placeholder'],
            'source_start': target['source_start'], 'source_end': target['source_end'],
            'candidates': page['candidates'], 'total': page['total'],
            'offset': offset, 'page_size': PAGE_SIZE, 'max_checked': MAX_CHOICES,
            'notice': 'Audition existing whole clips only. Normal playback and export choices are unchanged.'}


def render(app, data, status=lambda _: None):
    preview, target, choices, fingerprint = _choices(app, data)
    if data.get('fingerprint') != fingerprint:
        raise ValueError('The source, clip choices or playback settings changed. Refresh this comparison.')
    mode = data.get('mode', 'context')
    if mode not in ('sequence', 'context', 'together'):
        raise ValueError('Choose successive clips, repeated context, or simultaneous alternatives.')
    checked = data.get('candidate_ids')
    if (not isinstance(checked, list) or not 1 <= len(checked) <= MAX_CHOICES
            or any(not isinstance(i, str) for i in checked) or len(set(checked)) != len(checked)):
        raise ValueError('Check between 1 and 32 distinct alternatives.')
    contextual = getattr(choices, 'contextual', False)
    if any((i != 'silence' or contextual) and choices.find(i) is None for i in checked):
        raise ValueError('A checked clip is unavailable for this delimiter, voice or performance.')
    silence_ms = silence_duration(data.get('silence_ms', 200))
    gap_ms = silence_duration(data.get('comparison_gap_ms', 250), 'Pause between comparisons')
    if data.get('arrangement_id'):
        from arrangement_comparison import render as render_arrangement
        return render_arrangement(app, data, preview, target, checked, mode, silence_ms, gap_ms, fingerprint, status)
    segments = preview['segments'];index = segments.index(target)
    left, right = context_bounds(segments, target['order'], data.get('before_words', 5),
                                data.get('after_words', 5), data.get('scope', 'nearby'))
    cache = {};events = [];cursor = 0
    groups={g['slot_order']:g for g in preview.get('replacement_groups',{}).get('groups',[])}

    def sample(ident,segment):
        if ident == 'silence':return {'pcm': None, 'channels': 1, 'count': round(silence_ms*RATE/1000)}
        if ident not in cache:
            asset = app.store.asset(ident);assert_audio_usable(asset, app.store)
            digest = media_digest(asset)
            if asset.get('sha256') and digest != asset['sha256']:
                raise ValueError('A source clip changed after it was recorded. Review it before comparing.')
            channels = probe(asset['path'])['channels']
            if channels not in (1, 2):raise ValueError('Comparison supports mono and stereo clips.')
            pcm = decode(asset['path'], channels=channels)
            from clip_group_schema import effective_effects,effective_section_effects
            from audio_effects import apply_effects
            group=groups.get(segment['order'])
            if group is not None:
                pcm,_=apply_effects(pcm,channels,effective_effects(group,ident))
                pcm,_=apply_effects(pcm,channels,effective_section_effects(group,ident))
            cache[ident] = {'pcm': pcm, 'channels': channels, 'count': len(pcm)//channels,
                            'asset': asset, 'sha256': digest}
        return cache[ident]

    def normal(segment):
        if segment.get('speech_excluded') or segment['type'] == 'fixed' and not segment['spoken_text']:
            return None
        selected = data.get('selections', {}).get(str(segment['order']))
        options = segment['options']
        if selected == 'silence':
            if segment['type'] == 'fixed':raise ValueError('Fixed speech cannot be replaced by silence.')
            return 'silence'
        if selected:
            if not any(c['id'] == selected for c in options):
                raise ValueError('A normal context selection no longer matches this dialogue.')
            return selected
        if options:return options[0]['id']
        if segment['type'] != 'fixed':return 'silence'
        raise ValueError('Fixed context speech is missing. Choose clips-only scope or supply its recording.')

    def emit(ident, segment, iteration, start=None, candidate=False):
        nonlocal cursor
        if ident is None:return
        clip = sample(ident,segment);begin = cursor if start is None else start
        events.append({'asset_id': None if ident == 'silence' else ident,
                       'kind': 'silence' if ident == 'silence' else 'audio',
                       'slot_order': segment['order'], 'candidate_id': ident if candidate else None,
                       'iteration': iteration, 'start_sample': begin,
                       'end_sample': begin+clip['count'], 'sample_count': clip['count'],
                       'source_in_sample': 0, 'source_out_sample': clip['count'],
                       'track_id': 'candidate:'+ident if candidate else 'context:'+str(segment['order'])})
        if start is None:cursor += clip['count']

    def context(parts, iteration):
        for segment in parts:emit(normal(segment), segment, iteration)

    status('Scheduling complete comparison clips…')
    if mode == 'context':
        for iteration, ident in enumerate(checked):
            if iteration:cursor += round(gap_ms*RATE/1000)
            context(segments[left:index], iteration)
            emit(ident, target, iteration, candidate=True)
            context(segments[index+1:right+1], iteration)
    else:
        context(segments[left:index], 0)
        if mode == 'sequence':
            for ident in checked:emit(ident, target, 0, candidate=True)
        else:
            start = cursor
            for ident in checked:emit(ident, target, 0, start=start, candidate=True)
            cursor = max(e['end_sample'] for e in events)
        context(segments[index+1:right+1], 0)
    if not cursor:return {'no_audio': True, 'notice': 'All selected alternatives were skipped; no audio samples.'}
    if cursor > RATE*MAX_AUDIO_SECONDS:raise ValueError('Comparison exceeds the current 30-minute preview limit.')
    channels = max((c['channels'] for c in cache.values()), default=1)
    pcm = array.array('f', [0])*(cursor*channels)
    for event in events:
        if event['kind'] == 'silence':continue
        clip = cache[event['asset_id']];source = clip['pcm'];begin = event['start_sample']
        if channels == clip['channels']:
            for i, value in enumerate(source):pcm[begin*channels+i] += value
        else:
            for i, value in enumerate(source):pcm[(begin+i)*2] += value;pcm[(begin+i)*2+1] += value
    peak = max((abs(value) for value in pcm), default=0)
    master_gain = min(1, .98/peak) if peak else 1
    if master_gain < 1:pcm = array.array('f', (v*master_gain for v in pcm))
    if _choices(app, data)[3] != fingerprint or any(media_digest(c['asset']) != c['sha256'] for c in cache.values()):
        raise ValueError('The source or audio changed during comparison. No preview was retained.')
    for asset_id in cache:assert_audio_usable(app.store.asset(asset_id), app.store)
    ident = uuid.uuid4().hex;path = app.folder/'comparison-previews'/(ident+'.flac')
    try:
        encode_preview_pcm(pcm, path, channels=channels)
        result = {'id': ident, 'kind': 'comparison-preview', 'path': str(path),
                  'seconds': cursor/RATE, 'sample_count': cursor, 'sample_rate': RATE,
                  'channels': channels, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'words': [], 'timing_source': 'whole-clip comparison positions, not word alignment',
                  'comparison': {'mode': mode, 'scope': data.get('scope', 'nearby'),
                      'slot_order': target['order'], 'events': events, 'fingerprint': fingerprint,
                      'candidate_ids': checked, 'master_gain': master_gain,
                      'whole_clips_preserved': True, 'delivery_selection_unchanged': True},
                  'notice': 'Audition only. Export uses the normal selected clips; no TTS was requested.'}
        return app.store.add_asset(result)
    except Exception:
        path.unlink(missing_ok=True)
        raise
