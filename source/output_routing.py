"""Explicit multichannel output matrices for workstation/device adapters.

Channel numbers are one-based, matching audio-interface labels. This module
does not select a system device, install a driver, or change its default route.
"""
import array
import math
from track_bus_routing import route_pcm


def validate_assignment(destinations):
    if not isinstance(destinations,list) or len(destinations) not in (0,1,2) or any(type(c) is not int or c<1 for c in destinations):
        raise ValueError('Choose Off, one positive mono channel or two stereo channels per track.')
    if len(set(destinations))!=len(destinations):
        raise ValueError('Choose distinct left and right output channels.')
    return destinations


def validate_routes(tracks,routes,channels):
    if type(channels) is not int or channels<1:raise ValueError('Output channel count must be a positive integer.')
    if not isinstance(routes,dict) or set(routes)-{t['id'] for t in tracks}:raise ValueError('Choose existing tracks for output routing.')
    for ident,destinations in routes.items():
        validate_assignment(destinations)
        if any(type(c) is not int or not 1<=c<=channels for c in destinations):raise ValueError('The track route exceeds the selected interface channel count.')
    return routes


def mix_outputs(metadata,track_signals,routes,output_channels):
    validate_routes(metadata['tracks'],routes,output_channels)
    count=metadata['sample_count'];source_channels=metadata['channels']
    result=array.array('f',[0])*(count*output_channels)
    for track,signal in track_signals:
        destinations=routes.get(track['id'],[])
        if not destinations:continue
        if len(signal)!=count*source_channels:raise ValueError('Track output does not match the shared playback clock.')
        if len(destinations)==2 and source_channels==1:signal=route_pcm(signal,1,2,0,0,False);channels=2
        else:channels=source_channels
        for i in range(count):
            if len(destinations)==1:
                value=sum(signal[i*channels:i*channels+channels])/channels
                result[i*output_channels+destinations[0]-1]+=value
            else:
                result[i*output_channels+destinations[0]-1]+=signal[i*channels]
                result[i*output_channels+destinations[1]-1]+=signal[i*channels+1]
    if any(not math.isfinite(v) for v in result):raise ValueError('Output routing produced non-finite audio.')
    peak=max((abs(v) for v in result),default=0)
    return result,dict(channels=output_channels,sample_count=count,peak=peak,over_unity=peak>1,
        routes=routes,clock_sample_rate=44100,processing='post-track-effects, post-fader and post-preview-master; unity routing; no extra normalization')
