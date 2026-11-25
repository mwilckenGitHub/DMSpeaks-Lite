"""Experimental non-destructive, time-scoped processing of one PCM stream.

The grouped-preview and checked-export workflows use this module. It processes
one merged active window at a time and preserves each effect's generated tail.
"""
import array
import math

from audio_effects import apply_effects
from effects_schema import validate_effects
from engine_limits import MAX_AUDIO_SECONDS
from processing_scope import plan
from stereo_frames import frame_count, trim_frames

RATE = 44100


def apply(pcm, channels, clips, effects, *, strength=1.0, fade_samples=220):
    """Blend an effect chain into selected half-open ranges; do not mutate input.

    The input to each chain is faded at the range edges. Subtracting that dry
    input from its processed output retains the original signal and lets an
    effect's tail decay beyond the selected range. Overlapping ranges share one
    application of the chain; disjoint ranges are processed independently.
    """
    count = frame_count(pcm, channels)
    if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('Strength must be between zero and one.')
    if type(fade_samples) is not int or not 0 <= fade_samples <= 5 * RATE:
        raise ValueError('Transition length must be between zero and five seconds.')
    if count > RATE * MAX_AUDIO_SECONDS:
        raise ValueError('Audio exceeds the timeline capacity.')
    if any(not math.isfinite(value) for value in pcm):
        raise ValueError('Audio must contain finite samples.')
    nodes = validate_effects(effects)
    coverage = plan([{'id': 'processor'}], 'processor', count, clips=clips)['coverage']
    result = array.array('f', pcm)
    if not strength or not any(node['enabled'] for node in nodes):
        return result, {'coverage': coverage, 'tail_samples': 0}
    furthest = count
    for window in coverage:
        start, end = window['start_sample'], window['end_sample']
        source = trim_frames(pcm, channels, start, end)
        length = end - start
        fade = min(fade_samples, length // 2)
        if fade > 1:
            for frame in range(fade):
                weight = math.sin(math.pi * frame / (2 * (fade - 1))) ** 2
                for channel in range(channels):
                    source[frame * channels + channel] *= weight
                    source[(length - 1 - frame) * channels + channel] *= weight
        processed, _ = apply_effects(source, channels, nodes)
        processed_frames = frame_count(processed, channels)
        required = start + processed_frames
        if required > RATE * MAX_AUDIO_SECONDS:
            raise ValueError('The effect tail exceeds the timeline capacity.')
        if required > len(result) // channels:
            result.extend(array.array('f', [0]) * ((required - len(result) // channels) * channels))
        for offset, value in enumerate(processed):
            dry = source[offset] if offset < len(source) else 0.0
            result[start * channels + offset] += strength * (value - dry)
        furthest = max(furthest, required)
    if any(not math.isfinite(value) for value in result):
        raise ValueError('The processing layer produced invalid samples.')
    return result, {'coverage': coverage, 'tail_samples': furthest - count}
