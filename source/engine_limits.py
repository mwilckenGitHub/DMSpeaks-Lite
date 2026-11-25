"""Runtime capacity is separate from corpus schemas and legacy CSV layouts."""
import os

MAX_COMPARISON_CHOICES = 32
# Silence uses the same supported duration as an imported source clip. This is
# the current in-memory editor's media capacity, not a corpus-format restriction.
MAX_AUDIO_SECONDS = 1800
MAX_SILENCE_MS = MAX_AUDIO_SECONDS * 1000


def silence_duration(value, label='Silence duration'):
    if type(value) is not int or not 0 <= value <= MAX_SILENCE_MS:
        raise ValueError(f'{label} must be an integer from 0 to {MAX_SILENCE_MS} ms (30 minutes).')
    return value

# The present recursive dependency scheduler is verified below Python's stack
# limit. These are implementation limits; imported corpus data is not truncated.
try:
    MAX_SEGMENTS = int(os.environ.get('DMSPEAKS_MAX_SEGMENTS', '256'))
except ValueError:
    raise ValueError('DMSPEAKS_MAX_SEGMENTS must be an integer from 1 to 512.') from None
if not 1 <= MAX_SEGMENTS <= 512:
    raise ValueError('DMSPEAKS_MAX_SEGMENTS must be an integer from 1 to 512.')


def capabilities():
    return {'max_dialogue_segments': MAX_SEGMENTS,
            'max_comparison_choices': MAX_COMPARISON_CHOICES,
            'max_audio_seconds': MAX_AUDIO_SECONDS, 'max_silence_ms': MAX_SILENCE_MS}
