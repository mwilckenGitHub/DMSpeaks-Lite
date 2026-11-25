"""Export and render independent background choices with the shared DSP engine."""
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from guide_bindings import parent_id,binding

FORMAT='dmspeaks-background-library'
VERSION='0.1'


def choices_for_arrangement(asset_id,layers,tracks,store):
    choices=[]
    for track in tracks:
        members=[layer for layer in layers if layer['track_id']==track['id']]
        background=[layer for layer in members if layer.get('clip_role') in {'background_music','background_ambience'}]
        if not background:continue
        ids={layer['id'] for layer in background}
        issue=''
        if len(background)!=len(members):issue='This track mixes speech and backgrounds; keep it as a frozen arrangement or move backgrounds to their own track.'
        elif any(parent_id(layer) is not None and parent_id(layer) not in ids for layer in background):issue='This background depends on another track. Use an independent anchor before exporting it as a runtime choice.'
        assets={}
        for layer in background:
            source=store.asset(layer['asset_id'])
            assets[source['id']]=dict(id=source['id'],audio_path=layer['source_audio_path'],
                audio_sha256=layer['source_audio_sha256'],sample_count=source['sample_count'],channels=source['channels'])
        choices.append(dict(id=hashlib.sha256((asset_id+'\0'+track['id']).encode()).hexdigest()[:24],
            label=track.get('name') or track['id'],role=track.get('role','music'),eligible=not issue,issue=issue,
            origin_arrangement=asset_id,version=VERSION,tracks=[copy.deepcopy(track)],
            layers=[{k:copy.deepcopy(v) for k,v in layer.items() if k not in {'waveform','track_bus_id','stem_stage'}} for layer in background],
            sources=list(assets.values()),processing='Original source bytes and editable graph. Apply once using the shared background engine; never play these in addition to a frozen background stem.'))
    return choices


def render_choices(choices,resolve_source,speech_regions,fit_samples=None,limit_bytes=256*1024*1024):
    """No editor/store initialization, speech synthesis or original-file writes."""
    from layered_audio import render_layers
    layers=[];tracks=[];assets={};estimated=0
    for choice in choices:
        if choice.get('version')!=VERSION or not choice.get('eligible'):
            raise ValueError(choice.get('issue') or 'Unsupported background choice.')
        prefix=choice['id']+':'
        for source in choice['sources']:
            frames=source.get('sample_count');channels=source.get('channels')
            if type(frames) is not int or frames<=0 or channels not in (1,2):raise ValueError('Invalid background source metadata.')
            estimated+=frames*channels*4
            path=resolve_source(source)
            assets[prefix+source['id']]=dict(id=prefix+source['id'],kind='background-source',path=str(path),
                sha256=source['audio_sha256'],sample_count=frames,channels=channels,words=[])
        for raw in choice['tracks']:
            track=copy.deepcopy(raw);track['id']=prefix+track['id'];tracks.append(track)
        for raw in choice['layers']:
            layer=copy.deepcopy(raw);layer['id']=prefix+layer['id'];layer['track_id']=prefix+layer['track_id'];layer['asset_id']=prefix+layer['asset_id']
            if layer.get('after_layer_id') is not None:layer['after_layer_id']=prefix+layer['after_layer_id']
            if binding(layer):layer['guide_binding']['target_layer_id']=prefix+layer['guide_binding']['target_layer_id']
            policy=layer.get('background_loop')
            if policy and policy.get('enabled'):
                if fit_samples is not None:
                    span=fit_samples-layer.get('anchor_start_sample',0)
                    if span<=0:raise ValueError('A fitted loop starts after the end of this dialogue.')
                    previous=policy['span_samples'];policy['span_samples']=span
                    if layer.get('source_out_sample')==previous:layer['source_out_sample']=span
                    if layer.get('source_in_sample',0)>=span or layer.get('source_out_sample',0)>span:raise ValueError('This fitted loop is shorter than its trimmed sections.')
                estimated+=policy['span_samples']*2*4
            layers.append(layer)
    if estimated>limit_bytes:raise ValueError('Selected background sources/spans exceed the 256 MiB processing limit.')
    pcm,metadata=render_layers(layers,tracks,SimpleNamespace(asset=lambda ident:assets[ident]),
        speech_regions=speech_regions,normalize_master=False)
    if len(pcm)*4>limit_bytes:raise ValueError('Background mix exceeds the 256 MiB playback cache limit.')
    return pcm,metadata
