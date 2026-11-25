# Local Qwen draft, reviewed for strict channel validation and generic PCM input.
"""Stereo frame utilities for interleaved PCM data."""
import math
import array

def frame_count(pcm, channels):
    """Return number of complete frames.

    Args:
        pcm: array of samples
        channels: 1 or 2
    Returns:
        int: frame count
    """
    if type(channels) is not int or channels not in (1, 2):
        raise ValueError("channels must be 1 or 2")
    if len(pcm) % channels != 0:
        raise ValueError("pcm length not divisible by channels")
    return len(pcm) // channels

def trim_frames(pcm, channels, begin, end):
    """Extract frames [begin, end).

    Args:
        pcm: array of samples
        channels: 1 or 2
        begin: start frame index
        end: end frame index
    Returns:
        array: trimmed samples
    """
    if not isinstance(begin, int) or isinstance(begin, bool):
        raise TypeError("begin must be int")
    if not isinstance(end, int) or isinstance(end, bool):
        raise TypeError("end must be int")
    if begin < 0 or end < 0:
        raise ValueError("indices must be non-negative")
    if begin > end:
        raise ValueError("begin must be <= end")
    total = frame_count(pcm, channels)
    if begin > total or end > total:
        raise ValueError("index out of range")
    start_sample = begin * channels
    end_sample = end * channels
    return array.array('f', pcm[start_sample:end_sample])

def pan_frame(left, right, pan, source_channels):
    """Apply pan/balance to stereo frame.

    Pan: -1..1, -1=left, 0=center, 1=right (constant power)
    Balance: negative attenuates R, positive attenuates L

    Args:
        left: left sample
        right: right sample
        pan: float -1..1
        source_channels: 1 or 2
    Returns:
        tuple: (left_out, right_out)
    """
    if isinstance(pan, bool) or not isinstance(pan, (int, float)):
        raise TypeError("pan must be numeric")
    if not math.isfinite(pan):
        raise ValueError("pan must be finite")
    if pan < -1 or pan > 1:
        raise ValueError("pan must be in [-1, 1]")

    if type(source_channels) is not int or source_channels not in (1,2):
        raise ValueError("source_channels must be 1 or 2")
    if source_channels == 1:
        angle = (pan + 1) * math.pi / 4
        l = left * math.cos(angle)
        r = left * math.sin(angle)
        return (l, r)
    elif source_channels == 2:
        if pan == 0:
            return (left, right)
        elif pan < 0:
            factor = math.cos(abs(pan) * math.pi / 2)
            return (left, right * factor)
        else:
            factor = math.cos(pan * math.pi / 2)
            return (left * factor, right)
    else:
        raise ValueError("source_channels must be 1 or 2")


def convert_channels(pcm, source_channels, target_channels):
    if type(source_channels) is not int or source_channels not in (1, 2):
        raise ValueError("source_channels must be 1 or 2")
    if type(target_channels) is not int or target_channels not in (1, 2):
        raise ValueError("target_channels must be 1 or 2")
    if not isinstance(pcm, array.array):
        raise TypeError("pcm must be array.array")
    if pcm.typecode != 'f':
        raise TypeError("pcm must be float array")
    if len(pcm) % source_channels != 0:
        raise ValueError("pcm length not divisible by source_channels")
    frames = len(pcm) // source_channels
    if source_channels == target_channels:
        return array.array('f', pcm)
    out = array.array('f')
    if source_channels == 2 and target_channels == 1:
        for i in range(frames):
            l = pcm[i * 2]
            r = pcm[i * 2 + 1]
            out.append((l + r) / 2.0)
    elif source_channels == 1 and target_channels == 2:
        for i in range(frames):
            s = pcm[i]
            out.append(s)
            out.append(s)
    return out
