"""Nondestructive virtual loop sources on the 44.1 kHz authoring clock."""
import array
import copy
from engine_limits import MAX_AUDIO_SECONDS
from stereo_frames import frame_count,trim_frames

RATE=44100


def validate_loop(value, source_frames):
    if value is None:
        return None
    if not isinstance(value,dict) or value.get('version','0.1')!='0.1':
        raise ValueError('Unsupported background-loop policy.')
    enabled=value.get('enabled',False)
    if type(enabled) is not bool:
        raise ValueError('Background loop enabled must be true or false.')
    start=value.get('start_sample',0);end=value.get('end_sample',source_frames)
    span=value.get('span_samples',source_frames)
    if any(type(v) is not int for v in (start,end,span)) or not 0<=start<end<=source_frames or not 1<=span<=RATE*MAX_AUDIO_SECONDS:
        raise ValueError('Choose a nonempty loop region inside the original source and a span up to 30 minutes.')
    return dict(version='0.1',enabled=enabled,start_sample=start,end_sample=end,span_samples=span)


def loop_source(source,channels,value,speed,stretch):
    """Tempo affects the loop unit; the virtual span retains its exact clock.

    Clips trim/split this virtual source, so sections reference the same signal
    history. Envelopes and effects run later, once across the full span.
    """
    count=frame_count(source,channels);policy=validate_loop(value,count)
    if not policy or not policy['enabled']:
        return source,None
    unit=stretch(trim_frames(source,channels,policy['start_sample'],policy['end_sample']),speed,channels)
    frames=frame_count(unit,channels)
    if not frames:
        raise ValueError('The chosen loop region has no samples after the tempo change.')
    repeat,remainder=divmod(policy['span_samples'],frames)
    output=unit*repeat+unit[:remainder*channels]
    return array.array('f',output),dict(policy=copy.deepcopy(policy),source_media_sample_count=count,
        tempo=speed,loop_unit_sample_count=frames,virtual_sample_count=policy['span_samples'],
        clock_sample_rate=RATE,processing='repeat complete unit then final partial unit; effects and fades applied once')
