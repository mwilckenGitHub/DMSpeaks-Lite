"""Local Qwen routing draft, corrected and verified for unity mono output."""
import array
import math
from stereo_frames import frame_count,pan_frame


def route_pcm(pcm,source_channels,output_channels,gain_db,pan,muted):
    count=frame_count(pcm,source_channels)
    if not isinstance(pcm,array.array) or pcm.typecode!='f':raise ValueError('Use floating-point PCM.')
    if type(output_channels) is not int or output_channels not in (1,2):raise ValueError('Use mono or stereo track output.')
    for value,lo,hi,label in [(gain_db,-60,12,'gain'),(pan,-1,1,'pan')]:
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:raise ValueError('Invalid track '+label+'.')
    if not isinstance(muted,bool):raise ValueError('Track mute must be true or false.')
    if output_channels==1 and (source_channels!=1 or pan!=0):raise ValueError('Use stereo output to retain stereo/pan information.')
    gain=10**(gain_db/20)
    if muted:return array.array('f',[0])*(count*output_channels)
    # Constant-power panning applies when spreading mono into stereo. A mono
    # output must retain unity level instead of taking a panned left channel.
    if output_channels==1:return array.array('f',(v*gain for v in pcm))
    left,right=pan_frame(1,1,pan,source_channels);result=array.array('f')
    for i in range(count):
        l=pcm[i*source_channels];r=pcm[i*source_channels+1] if source_channels==2 else l
        result.extend((l*gain*left,r*gain*right))
    return result
