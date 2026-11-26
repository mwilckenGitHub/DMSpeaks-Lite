"""Join measured dialogue clips without shifting their sample clock."""

import numpy as np


def join_reviewed_clips(parts, soften_after=(), *, taper_frames=176):
    """Keep every frame and soften only the approved boundaries.

    Each boundary identifies the number of clips before it. Inputs are mono
    normalized PCM on the same clock. Continuous boundaries need no taper.
    """
    clips = [np.asarray(part, dtype=np.float32) for part in parts]
    if len(clips) < 2 or any(clip.ndim != 1 or not clip.size or not np.isfinite(clip).all() for clip in clips):
        raise ValueError("A spoken join requires at least two finite mono recordings.")
    if type(taper_frames) is not int or taper_frames < 2:
        raise ValueError("The join taper must span at least two samples.")
    boundaries = set(soften_after)
    if any(type(index) is not int or not 0 < index < len(clips) for index in boundaries):
        raise ValueError("A spoken join taper points outside the recording.")
    clock = [0, *np.cumsum([clip.size for clip in clips])]
    result = np.concatenate(clips)
    for boundary in sorted(boundaries):
        width = min(taper_frames, clips[boundary - 1].size // 2, clips[boundary].size // 2)
        if width < 2:
            raise ValueError("The measured recording is too short to taper its join.")
        frame = clock[boundary]
        gain = (.5 - .5 * np.cos(np.linspace(0, np.pi, width))).astype(np.float32)
        result[frame - width:frame] *= gain[::-1]
        result[frame:frame + width] *= gain
    return result
