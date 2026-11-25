"""Continuous pre-fader track effects, separate from source/section processing.

Clips keep their source and dependency clocks. A track bus processes their sum
once and owns its final decay; it never inserts that decay between clips.
"""
import array
import math
from audio_effects import apply_effects
from effects_schema import validate_effects
from stereo_frames import frame_count
from engine_limits import MAX_AUDIO_SECONDS

RATE=44100


def validate_track(track):
    chain=validate_effects(track.get('bus_effects'))
    if 'bus_effects_bypassed' in track and not isinstance(track['bus_effects_bypassed'],bool):
        raise ValueError('Continuous track-effect bypass must be true or false.')
    return [] if track.get('bus_effects_bypassed',False) else chain


def waveform(pcm,channels):
    count=frame_count(pcm,channels);step=max(1,math.ceil(count/800));peaks=[];split=[[] for _ in range(channels)]
    for begin in range(0,count,step):
        per=[max((abs(pcm[i*channels+c]) for i in range(begin,min(count,begin+step))),default=0) for c in range(channels)]
        peaks.append(max(per))
        for c in range(channels):split[c].append(per[c])
    return dict(peaks=peaks,channel_peaks=split,channels=channels,sample_count=count,
                step_samples=step,sample_rate=RATE,stage='continuous track effects and fader before master level')


def render(layers,tracks,chunks,output_channels):
    """Return bus metadata and post-fader PCM; caller mixes/master-scales once."""
    from track_bus_routing import route_pcm
    outputs={};buses=[];solo=any(t.get('solo',False) for t in tracks)
    for layer in layers:
        layer.pop('track_bus_id',None);layer.pop('stem_stage',None)
        if 'bypass_bus_effects' in layer and not isinstance(layer['bypass_bus_effects'],bool):
            raise ValueError('Clip continuous-effect bypass must be true or false.')
    for track in tracks:
        chain=validate_track(track)
        if not any(node['enabled'] for node in chain):continue
        members=[layer for layer in layers if layer['track_id']==track['id'] and layer['kind']=='audio'
                 and layer['rendered_sample_count']>0 and not layer.get('bypass_bus_effects',False)]
        if not members:continue
        start=min(layer['start_sample'] for layer in members);stop=max(layer['end_sample'] for layer in members)
        channels=max(layer['source_channels'] for layer in members)
        signal=array.array('f',[0])*((stop-start)*channels)
        for layer in members:
            layer['track_bus_id']='bus:'+track['id'];layer['stem_stage']='pre_continuous_track_effects'
            # Mono clips entering a stereo bus use a centered constant-power
            # send. Native stereo clips keep their channel identity.
            if layer.get('muted'):continue
            source=route_pcm(chunks[layer['id']],layer['source_channels'],channels,0,0,False)
            at=(layer['start_sample']-start)*channels
            for i,value in enumerate(source):signal[at+i]+=value
        processed,info=apply_effects(signal,channels,chain)
        length=frame_count(processed,channels)
        if start+length>RATE*MAX_AUDIO_SECONDS:raise ValueError('The continuous track-effect tail exceeds the editor timeline capacity.')
        muted=bool(track.get('muted') or solo and not track.get('solo'))
        output=route_pcm(processed,channels,output_channels,track.get('gain_db',0),track.get('pan',0),muted)
        ident='bus:'+track['id'];outputs[ident]=output
        buses.append(dict(id=ident,track_id=track['id'],member_layer_ids=[layer['id'] for layer in members],
            start_sample=start,dry_end_sample=stop,end_sample=start+length,
            dry_rendered_sample_count=stop-start,rendered_sample_count=length,
            effect_tail_sample_count=length-(stop-start),effects=info['effects'],effects_engine=info['effects_engine'],
            processing_channels=channels,channels=output_channels,gain_db=track.get('gain_db',0),pan=track.get('pan',0),muted=muted,
            waveform=waveform(output,output_channels)))
    return buses,outputs
