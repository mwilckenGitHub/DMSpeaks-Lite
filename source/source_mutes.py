"""Non-destructive source-clock mute envelopes; supervised local Qwen scaffold.

Positions describe manual gain automation, never inferred acoustic approval.
"""
import array
import math


def apply_source_mutes(pcm, channels, windows):
    if type(channels) is not int or channels not in (1, 2):
        raise ValueError('Use one or two channels')
    if not isinstance(pcm, array.array) or pcm.typecode != 'f' or len(pcm) % channels:
        raise ValueError('Use complete interleaved float32 frames')
    if not all(math.isfinite(value) for value in pcm):
        raise ValueError('Nonfinite source PCM')
    if not isinstance(windows, list):
        raise ValueError('Use a window array')
    frames = len(pcm) // channels
    seen, positions = set(), []
    for window in windows:
        if not isinstance(window, dict):
            raise ValueError('Invalid mute window')
        ident = window.get('id')
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError('Mute windows need unique nonempty string IDs')
        seen.add(ident)
        values = [window.get(key) for key in ('fade_down_start', 'silence_start', 'silence_end', 'fade_up_end')]
        if any(type(value) is not int for value in values):
            raise ValueError('Mute positions must be integer source frames')
        down, start, end, up = values
        if not 0 <= down <= start < end <= up <= frames:
            raise ValueError('Invalid mute-window bounds')
        if start != 0 and start - down < 2 or end != frames and up - end < 2:
            raise ValueError('Interior mute boundaries require a ramp')
        positions.append(values)
    if not positions:
        return array.array('f', pcm)
    gains = array.array('f', [1]) * frames
    for down, start, end, up in positions:
        for frame in range(down, start):
            gains[frame] = min(gains[frame], math.cos(math.pi / 2 * (frame - down) / (start - down)) ** 2)
        for frame in range(start, end):
            gains[frame] = 0
        for frame in range(end, up):
            gains[frame] = min(gains[frame], math.sin(math.pi / 2 * (frame - end) / (up - end)) ** 2)
    result = array.array('f', pcm)
    for frame, gain in enumerate(gains):
        if gain != 1:
            for channel in range(channels):
                result[frame * channels + channel] *= gain
    return result
