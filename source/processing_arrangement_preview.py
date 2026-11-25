"""Experimental effect-stack audition from an existing arrangement.

This adapter renders existing track outputs and applies the experimental
processing stack without modifying source media. The grouped-preview and
checked-export workflows use its result; source recordings remain unchanged.
"""
from processing_stack import render
from processing_definition import validate
from track_outputs import render_track_outputs
from join_edge_review import mark_join_edges, speech_clip_audible
import math
from pathlib import Path

# Keep this experiment small until its memory use is measured in the application.
MAX_INTERLEAVED_TRACK_SAMPLES = 8_000_000
MAX_AUDITION_SECONDS = 30
MAX_AUDITION_TRACKS = 8
MAX_AUDITION_LAYERS = 64
MAX_SOURCE_SECONDS = 60
MAX_SOURCE_BYTES = 64 * 1024 * 1024


def check_source_budget(layers, store):
    """Inspect source files before the regular renderer decodes whole recordings.

    This is a conservative prototype limit, not a hard process memory quota.
    Count repeated source uses separately because the renderer decodes each clip.
    """
    from audio import probe
    durations = {}
    total = 0.0
    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError('Choose valid audio clips.')
        loop = layer.get('background_loop')
        if loop is not None:
            if not isinstance(loop,dict) or layer.get('clip_role') not in {'background_music','background_ambience'}:
                raise ValueError('Looped background audio requires a music or ambience clip.')
            if loop.get('enabled') is True:
                span = loop.get('span_samples')
                if type(span) is not int or not 0 < span <= MAX_AUDITION_SECONDS * 44100:
                    raise ValueError(f'Looped background audio in Audio Lab supports a span up to {MAX_AUDITION_SECONDS} seconds.')
                total += span / 44100
        if layer.get('kind') in ('silence', 'direction'):
            count = layer.get('original_sample_count', 0)
            if type(count) is not int or not 0 <= count <= MAX_AUDITION_SECONDS * 44100:
                raise ValueError('A clip is too long for the experimental audition.')
            total += count / 44100
        else:
            source = store.asset(layer.get('asset_id'))
            path = Path(source['path'])
            if path not in durations:
                if path.stat().st_size > MAX_SOURCE_BYTES:
                    raise ValueError('A source file is too large for the experimental audition.')
                seconds = probe(path)['seconds']
                if not math.isfinite(seconds) or seconds <= 0:
                    raise ValueError('A source recording has an invalid duration and cannot be auditioned.')
                if seconds > MAX_AUDITION_SECONDS:
                    raise ValueError(f'A source recording is {seconds:.1f} seconds; this audition currently supports source recordings up to {MAX_AUDITION_SECONDS} seconds, even when only part is used.')
                durations[path] = seconds
            total += durations[path]
        if total > MAX_SOURCE_SECONDS:
            raise ValueError(f'The source recordings are too long in total for this audition ({total:.1f} seconds; limit {MAX_SOURCE_SECONDS} seconds). Repeated clips count separately.')


def preview(asset, store, stack):
    if not isinstance(asset, dict) or not isinstance(asset.get('layers'), list) or not isinstance(asset.get('tracks'), list):
        raise ValueError('Choose an editable arrangement.')
    from content_review import is_rejected
    if is_rejected(asset):
        raise ValueError('This recording was rejected in content review.')
    base_ids = [track.get('id') for track in asset['tracks']]
    if len(base_ids) > MAX_AUDITION_TRACKS or len(asset['layers']) > MAX_AUDITION_LAYERS:
        raise ValueError('This experimental audition supports up to eight tracks and 64 clips.')
    # A processed preview can be longer than its dry timeline due to an
    # effect tail. Re-verification must budget the dry source timeline.
    estimate = asset.get('dry_sample_count', asset.get('sample_count'))
    if type(estimate) is not int or not 0 < estimate <= MAX_AUDITION_SECONDS * 44100 or estimate * max(1, len(base_ids)) * 2 > MAX_INTERLEAVED_TRACK_SAMPLES:
        raise ValueError('This arrangement is too large for the experimental preview.')
    stack = validate(asset['tracks'], stack, estimate, layers=asset['layers'])
    check_source_budget(asset['layers'], store)
    meta, outputs = render_track_outputs(asset['layers'], asset['tracks'], store,
                                        audition_only=asset.get('audition_only') is True)
    if meta['sample_count'] * max(1, len(base_ids)) * meta['channels'] > MAX_INTERLEAVED_TRACK_SAMPLES:
        raise ValueError('This arrangement is too large for the experimental preview.')
    streams = {track['id']: pcm for track, pcm in outputs}
    result = render(stack, streams, meta['channels'], meta['sample_count'])
    result['definition'] = stack
    result['channels'] = meta['channels']
    result['source_master_gain'] = meta['master_gain']
    result['source_sample_count'] = meta['sample_count']
    tracks = {track['id']: track for track in meta['tracks']}
    any_solo = any(track.get('solo') for track in tracks.values())
    audible = {layer['id'] for layer in meta['layers']
               if speech_clip_audible(layer, tracks[layer['track_id']], any_solo)}
    mark_join_edges(result['mix'], result['channels'], meta['layers'], audible)
    result['join_edge_warnings'] = {layer['id']: layer['join_edge_warning'] for layer in meta['layers']
                                    if 'join_edge_warning' in layer}
    result['peak'] = max((abs(sample) for sample in result['mix']), default=0.0)
    result['exceeds_full_scale'] = result['peak'] > 1.0
    return result
