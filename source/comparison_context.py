"""Whole-clip context bounds; drafted locally by Qwen and checked by parent."""
import re


def rendered_window(first, last, total, before_frames, after_frames):
    """Keep the full replacement and take the available neighboring audio."""
    return max(0, first - before_frames), min(total, last + after_frames)


def audition_window_pcm(pcm, channels, window, replacement, rate):
    """Taper abrupt preview cuts within context, leaving the join and clip intact."""
    lo, hi = window
    first, last = replacement
    output = pcm[lo * channels:hi * channels]
    fade_frames = round(rate * .003)
    # Leave very short context windows intact instead of fading across all of
    # their speech. Natural dialogue boundaries already have their own shape.
    for edge, direction, available, cut in (
        (0, 1, first - lo, lo > 0),
        (hi - lo - 1, -1, hi - last, hi * channels < len(pcm)),
    ):
        if not cut or fade_frames < 2 or available < fade_frames:
            continue
        for distance in range(fade_frames):
            gain = distance / (fade_frames - 1)
            frame = (edge + direction * distance) * channels
            for channel in range(channels):
                output[frame + channel] *= gain
    return output


def context_bounds(segments, order, before=5, after=5, scope='nearby'):
    if not isinstance(segments, list) or not segments:
        raise ValueError('Choose a nonempty dialogue.')
    for count in (before, after):
        if type(count) is not int or not 0 <= count <= 50:
            raise ValueError('Context word targets must be integers from 0 to 50.')
    if scope not in ('clips', 'dialogue', 'nearby'):
        raise ValueError('Choose clips, nearby context, or whole dialogue.')
    matches = [i for i, s in enumerate(segments) if s.get('order') == order]
    if type(order) is not int or len(matches) != 1:
        raise ValueError('Choose one delimiter occurrence.')
    index = matches[0]
    if scope == 'clips':return index, index
    if scope == 'dialogue':return 0, len(segments)-1
    pattern = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
    left = right = index
    count = 0
    while left > 0 and count < before:
        left -= 1
        count += len(pattern.findall(segments[left].get('spoken_text', '')))
    count = 0
    while right < len(segments)-1 and count < after:
        right += 1
        count += len(pattern.findall(segments[right].get('spoken_text', '')))
    return left, right
