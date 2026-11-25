"""Nondestructive effect stages after a retained processed-audio section.

A later split/copy captures a stage in history and windows its complete output.
Filters do not restart at the new boundary and each authored tail exists once.
"""
from audio_effects import apply_effects
from effects_schema import validate_effects
from stereo_frames import frame_count, trim_frames


def active(effects):
    return any(item['enabled'] for item in validate_effects(effects))


def process(chunk, channels, layer, info, dry, envelope, integer):
    source_dry = dry
    word_offset = 0
    total = frame_count(chunk, channels)
    track_tail = (total-info.get('track_effect_tail_sample_count', 0), total)
    edge_in = layer.get('fade_in_samples', 0)
    edge_out = info.get('edge_fade_out_samples', 0)

    def envelopes(signal, items):
        if not isinstance(items, list):raise ValueError('Invalid section envelopes.')
        for item in items:
            if not isinstance(item, dict):raise ValueError('Invalid section envelope.')
            total=frame_count(signal,channels)
            first=integer(item.get('start_sample'),'section envelope in',0,total)
            follows=item.get('follow_end',False)
            if not isinstance(follows,bool):raise ValueError('Section follow-end must be true or false.')
            stop=total if follows else integer(item.get('end_sample'),'section envelope out',first,total)
            if follows:item['end_sample']=stop
            if stop==first:raise ValueError('Section envelopes must contain audio.')
            fade_in=integer(item.get('fade_in_samples',0),'section fade in')
            fade_out=integer(item.get('fade_out_samples',0),'section fade out')
            signal[first*channels:stop*channels]=envelope(trim_frames(signal,channels,first,stop),channels,fade_in,fade_out,0)
        return signal

    def window(signal, region):
        nonlocal dry, word_offset, track_tail, edge_in, edge_out
        from rendered_regions import bounds
        first,stop=bounds({'rendered_region':region})
        total=frame_count(signal,channels)
        follows=region.get('follow_end',False)
        if not isinstance(follows,bool):raise ValueError('Section follow-end must be true or false.')
        if follows:stop=total
        if stop>total or stop<=first:
            raise ValueError('Section out exceeds the processed recording. Adjust the section in/out times before shortening its processing.')
        if follows:region['end_sample']=stop
        fade_in=integer(region.get('fade_in_samples',0),'section fade in')
        fade_out=integer(region.get('fade_out_samples',0),'section fade out')
        dry=max(0,min(dry,stop)-min(dry,first))
        left,right=max(first,track_tail[0]),min(stop,track_tail[1])
        track_tail=(max(0,left-first),max(0,right-first)) if right>left else (0,0)
        word_offset+=first
        edge_in=fade_in or (edge_in if first==0 else 0)
        edge_out=fade_out or (edge_out if stop==total else 0)
        return envelope(trim_frames(signal,channels,first,stop),channels,fade_in,fade_out,0)

    def effects(signal, chain):
        nonlocal edge_out
        normalized=validate_effects(chain)
        applied=[] if layer.get('kind') in {'silence','direction'} else normalized
        result,details=apply_effects(signal,channels,applied)
        if details['effect_tail_sample_count']:edge_out=0
        return result,details

    history=layer.get('section_history',[])
    if not isinstance(history,list):raise ValueError('Section history must be an array of processing stages.')
    for stage in history:
        if not isinstance(stage,dict) or set(stage)-{'rendered_envelopes','rendered_region','effects','group_effects','group_id'}:
            raise ValueError('Invalid section processing stage.')
        if 'group_id' in stage and (not isinstance(stage['group_id'],str) or not stage['group_id']):raise ValueError('Invalid inherited group effect identity.')
        chunk=envelopes(chunk,stage.get('rendered_envelopes',[]))
        if stage.get('rendered_region') is not None:chunk=window(chunk,stage['rendered_region'])
        chunk,_=effects(chunk,stage.get('group_effects',[]))
        chunk,_=effects(chunk,stage.get('effects',[]))
    chunk=envelopes(chunk,layer.get('rendered_envelopes',[]))
    if layer.get('rendered_region') is not None:
        info.update(pre_region_sample_count=frame_count(chunk,channels),pre_region_dry_sample_count=dry)
        chunk=window(chunk,layer['rendered_region'])
    chunk,group_post=effects(chunk,layer.get('group_section_effects',[]))
    chunk,post=effects(chunk,layer.get('section_effects',[]))
    info.update(dry_rendered_sample_count=dry,effect_tail_sample_count=frame_count(chunk,channels)-dry,
                track_effect_tail_sample_count=max(0,track_tail[1]-track_tail[0]),
                edge_fade_in_samples=edge_in,edge_fade_out_samples=edge_out,
                word_source_dry_sample_count=source_dry,word_offset_samples=word_offset)
    info.update(section_effects_applied=post['effects'],group_section_effects_applied=group_post['effects'],section_effect_added_sample_count=post['effect_tail_sample_count']+group_post['effect_tail_sample_count'])
    return chunk,info,dry


def edited_history(layer, chains):
    """Edit only inherited individual racks; retain windows/group ownership."""
    import copy
    history=layer.get('section_history',[])
    if not isinstance(chains,list) or len(chains)!=len(history):
        raise ValueError('Choose one effect chain for each inherited processing stage.')
    result=copy.deepcopy(history)
    for stage,chain in zip(result,chains):stage['effects']=validate_effects(chain)
    return result
