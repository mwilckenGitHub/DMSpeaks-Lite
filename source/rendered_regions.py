"""Sample-exact sections of a processed clip, retaining its source and controls.

The window is measured after the original clip processing, including its tail.
Splitting/copying a window never restarts a filter or adds a new envelope.
"""
import copy


def bounds(layer):
    region = layer.get('rendered_region')
    if region is None:return 0, layer['rendered_sample_count']
    if (not isinstance(region, dict) or type(region.get('start_sample')) is not int or
            type(region.get('end_sample')) is not int or not 0 <= region['start_sample'] < region['end_sample']):
        raise ValueError('Choose valid section in/out sample positions.')
    return region['start_sample'], region['end_sample']


def section(layer, lo, hi):
    """Keep local rendered samples [lo, hi) of this immutable clip."""
    if type(lo) is not int or type(hi) is not int or not 0 <= lo < hi <= layer['rendered_sample_count']:
        raise ValueError('Select a section inside the clip.')
    start, _ = bounds(layer)
    result = copy.deepcopy(layer)
    from section_effects import active
    if active(layer.get('section_effects')) or active(layer.get('group_section_effects')):
        # Preserve the complete prior stage (including its tail) before taking
        # a new window. The new section gets its own initially empty rack.
        stage={
            'rendered_envelopes':copy.deepcopy(layer.get('rendered_envelopes',[])),
            'rendered_region':copy.deepcopy(layer.get('rendered_region')),
            'effects':copy.deepcopy(layer.get('section_effects',[]))}
        if layer.get('group_section_effects_id'):
            stage.update(group_id=layer['group_section_effects_id'],group_effects=copy.deepcopy(layer.get('group_section_effects',[])))
        result.setdefault('section_history',[]).append(stage)
        result.pop('group_section_effects',None);result.pop('group_section_effects_id',None)
        result.pop('rendered_envelopes',None)
        result['section_effects']=[]
        start=0
        previous=None
    else:previous = layer.get('rendered_region')
    if previous and (previous.get('fade_in_samples') or previous.get('fade_out_samples')):
        result.setdefault('rendered_envelopes',[]).append(copy.deepcopy(previous))
    result['rendered_region'] = {'start_sample':start+lo, 'end_sample':start+hi}
    if hi==layer['rendered_sample_count'] and (previous is None or previous.get('follow_end') or previous['end_sample']==layer.get('pre_region_sample_count')):
        result['rendered_region']['follow_end']=True
    dry = layer.get('dry_rendered_sample_count',layer['rendered_sample_count'])
    kept_dry = max(0,min(dry,hi)-min(dry,lo))
    result.update(start_sample=layer['start_sample']+lo,end_sample=layer['start_sample']+hi,
                  rendered_sample_count=hi-lo,dry_rendered_sample_count=kept_dry,
                  effect_tail_sample_count=hi-lo-kept_dry,
                  edge_fade_in_samples=layer.get('edge_fade_in_samples',layer.get('fade_in_samples',0)) if lo==0 else 0,
                  edge_fade_out_samples=layer.get('edge_fade_out_samples',0) if hi==layer['rendered_sample_count'] else 0)
    return result


def edit_region(layers, ident, command, tracks):
    layer = next(item for item in layers if item['id'] == ident)
    start, count = layer['start_sample'], layer['rendered_sample_count']
    followers = [item for item in layers if item.get('after_layer_id') == ident]
    if command['op'] == 'split':
        cut = command.get('split_sample')
        if type(cut) is not int or not start < cut < start+count:
            raise ValueError('Place the playhead inside this clip.')
        left, right = section(layer,0,cut-start), section(layer,cut-start,count)
        gap = 0
    else:
        lo, hi = command.get('start_sample'), command.get('end_sample')
        if type(lo) is not int or type(hi) is not int or not start <= lo < hi <= start+count:
            raise ValueError('Select a range inside the selected clip.')
        ripple = command.get('ripple',True)
        if type(ripple) is not bool:raise ValueError('Ripple must be on or off.')
        if lo == start and hi == start+count:
            from timeline_commands import apply_timeline_edit
            return apply_timeline_edit({'layers':layers,'tracks':tracks}, {'op':'delete','layer_id':ident,'ripple':ripple})[0]
        if lo == start:
            kept = section(layer,hi-start,count)
            if not ripple:kept.update(after_layer_id=None,anchor_start_sample=hi,gap_samples=0,overlap_samples=0)
            layers[layers.index(layer)] = kept;return layers
        if hi == start+count:
            kept = section(layer,0,lo-start)
            if not ripple:
                for child in followers:child.update(after_layer_id=None,anchor_start_sample=child['start_sample'],gap_samples=0,overlap_samples=0)
            layers[layers.index(layer)] = kept;return layers
        left, right = section(layer,0,lo-start), section(layer,hi-start,count)
        gap = 0 if ripple else hi-lo
    from timeline_commands import section_clip_id
    right.update(id=section_clip_id(layers, command),after_layer_id=ident,gap_samples=gap,overlap_samples=0)
    left.setdefault('slot',{})['section_id'] = ident
    right.setdefault('slot',{})['section_id'] = right['id']
    for child in followers:child['after_layer_id'] = right['id']
    at = layers.index(layer);layers[at:at+1] = [left,right]
    return layers


def join_regions(layers, layer):
    from section_join import join_processed_sections
    return join_processed_sections(layers, layer)
