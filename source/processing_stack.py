"""Experimental layered processing on already-aligned PCM track streams.

Track order is top to bottom. Processing tracks run in that order and affect
only ordinary audio streams in their configured direction. Coverage uses the
original timeline duration; a previous effect's extended tail is not implicitly
included. The grouped-preview and checked-export workflows use this renderer.
"""
import array
import math

from engine_limits import MAX_AUDIO_SECONDS
from processing_scope import plan
from processing_windows import apply
from stereo_frames import frame_count

RATE = 44100


def render(tracks, streams, channels, duration_samples):
    if not isinstance(tracks, list) or not tracks:
        raise ValueError('Choose a nonempty track stack.')
    if type(duration_samples) is not int or not 0 <= duration_samples <= RATE * MAX_AUDIO_SECONDS:
        raise ValueError('Invalid timeline duration.')
    if not isinstance(streams, dict):
        raise ValueError('Choose valid audio streams.')
    ids = [track.get('id') if isinstance(track, dict) else None for track in tracks]
    if any(not isinstance(ident, str) or not ident for ident in ids) or len(set(ids)) != len(ids):
        raise ValueError('Track IDs must be unique and nonempty.')
    processors = {track['id'] for track in tracks if 'processing' in track}
    if set(streams) - set(ids) or processors & set(streams):
        raise ValueError('Audio streams must belong to ordinary tracks.')
    # Validate the channel count even for an otherwise empty stack.
    frame_count([], channels)
    output = {}
    for track_id, pcm in streams.items():
        length = frame_count(pcm, channels)
        if length > duration_samples or any(not math.isfinite(value) for value in pcm):
            raise ValueError('Audio streams must contain finite samples within the timeline.')
        output[track_id] = array.array('f', pcm)
        output[track_id].extend(array.array('f', [0]) * ((duration_samples - length) * channels))
    applied = []
    for track in tracks:
        if track['id'] not in processors:
            continue
        spec = track['processing']
        if not isinstance(spec, dict) or set(spec) - {'direction', 'clips', 'effects', 'strength', 'fade_samples'}:
            raise ValueError('Invalid processing track settings.')
        scope = plan(tracks, track['id'], duration_samples,
                     direction=spec.get('direction', 'above'), clips=spec.get('clips'))
        # Validate effect settings even when there are currently no targets.
        apply(array.array('f'), channels, [], spec.get('effects', []),
              strength=spec.get('strength', 1.0), fade_samples=spec.get('fade_samples', 220))
        targets = [ident for ident in scope['target_track_ids'] if ident in output]
        for ident in targets:
            output[ident], _ = apply(output[ident], channels, scope['coverage'], spec.get('effects', []),
                                      strength=spec.get('strength', 1.0), fade_samples=spec.get('fade_samples', 220))
        applied.append({'track_id': track['id'], 'targets': targets, 'coverage': scope['coverage'],
                        'strength': spec.get('strength', 1.0)})
    length = max([duration_samples, *(frame_count(pcm, channels) for pcm in output.values())])
    mix = array.array('f', [0]) * (length * channels)
    for pcm in output.values():
        for i, value in enumerate(pcm):
            mix[i] += value
    if any(not math.isfinite(value) for value in mix):
        raise ValueError('The processed mix contains invalid samples.')
    return {'streams': output, 'mix': mix, 'processors': applied, 'sample_count': length}
