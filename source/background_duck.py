"""Shared scheduled-speech ducking; no acoustic detection or source mutation."""
import array
import math
from engine_limits import MAX_AUDIO_SECONDS


def validate_ducking(value):
    if value is None:
        value={}
    if not isinstance(value,dict) or value.get('version','0.1')!='0.1':
        raise ValueError('Unsupported background ducking policy.')
    enabled=value.get('enabled',False)
    if type(enabled) is not bool:
        raise ValueError('Ducking enabled must be true or false.')
    result=dict(version='0.1',enabled=enabled,trigger='scheduled_speech')
    if value.get('trigger','scheduled_speech')!='scheduled_speech':
        raise ValueError('Only scheduled speech is supported as a ducking trigger.')
    for key,default,lo,hi in [('reduction_db',-12,-48,0),('attack_ms',20,0,5000),
                             ('hold_ms',80,0,5000),('release_ms',250,0,10000)]:
        number=value.get(key,default)
        if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number) or not lo<=number<=hi:
            raise ValueError('Invalid ducking '+key+'.')
        result[key]=float(number)
    return result


def held_regions(intervals,rate,hold):
    if type(rate) is not int or not 8000<=rate<=192000:
        raise ValueError('Invalid ducking sample rate.')
    if not isinstance(intervals,(list,tuple)):
        raise ValueError('Speech regions must be a list of sample intervals.')
    result=[]
    for item in intervals:
        if not isinstance(item,(list,tuple)) or len(item)!=2 or any(type(v) is not int for v in item) or not 0<=item[0]<=item[1]<=rate*MAX_AUDIO_SECONDS:
            raise ValueError('Invalid scheduled speech interval.')
        if item[1]>item[0]:result.append((item[0],item[1]+hold))
    merged=[]
    for start,end in sorted(result):
        if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else:merged.append((start,end))
    return merged


def iter_duck_gains(intervals,start_frame,count,rate,policy):
    """One frame sweep after sorting; nearest windows define the lower envelope."""
    policy=validate_ducking(policy)
    if any(type(v) is not int or v<0 for v in (start_frame,count)):
        raise ValueError('Invalid ducking output span.')
    regions=held_regions(intervals,rate,round(policy['hold_ms']*rate/1000))
    if start_frame+count>rate*MAX_AUDIO_SECONDS:
        raise ValueError('Ducking output exceeds the timeline limit.')
    if not policy['enabled'] or not regions:
        yield from (1.0 for _ in range(count));return
    attack=round(policy['attack_ms']*rate/1000);release=round(policy['release_ms']*rate/1000)
    depth=10**(policy['reduction_db']/20)
    def gain(region,frame):
        start,end=region
        if frame<start:
            return 1-(1-depth)*max(0,1-(start-frame)/attack) if attack else 1
        if frame<end:return depth
        return depth+(1-depth)*min(1,(frame-end)/release) if release else 1
    following=0
    for frame in range(start_frame,start_frame+count):
        while following<len(regions) and regions[following][0]<=frame:following+=1
        result=1.0
        if following:result=min(result,gain(regions[following-1],frame))
        if following<len(regions):result=min(result,gain(regions[following],frame))
        yield result


def apply_duck(pcm,channels,start_frame,intervals,rate,policy):
    if type(channels) is not int or channels not in (1,2) or len(pcm)%channels:
        raise ValueError('Use complete mono or stereo ducking frames.')
    result=array.array('f',pcm)
    for frame,gain in enumerate(iter_duck_gains(intervals,start_frame,len(pcm)//channels,rate,policy)):
        for channel in range(channels):result[frame*channels+channel]*=gain
    return result
