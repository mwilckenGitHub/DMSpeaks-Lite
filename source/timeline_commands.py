"""Non-destructive arrangement commands; local Qwen outline corrected and verified.

Commands change graph/source ranges, never source audio or authored corpus text.
The renderer remains authoritative for resulting sample positions and effect tails.
"""
import copy
import re
import uuid
from engine_limits import MAX_SEGMENTS


def section_clip_id(layers, command):
    ident = command.get('new_section_id')
    if ident is None:
        return 'clip-' + uuid.uuid4().hex
    if (not isinstance(ident, str) or re.fullmatch(r'clip-[0-9a-f]{32}', ident) is None or
            any(layer['id'] == ident for layer in layers)):
        raise ValueError('The new clip ID is invalid or already in use.')
    return ident


def apply_timeline_edit(base, command, clipboard_layer=None, *, _defer_capacity=False):
    """Structural cuts detach affected guide identities at their measured place.

    Moving and choosing a different take keep guides meaningful. Dividing,
    joining or removing source sections can change which word an ID denotes;
    retain positions explicitly instead of silently binding a different word.
    """
    from guide_bindings import validate_graph,detach_touching
    validate_graph(base.get('layers', []))
    working=copy.deepcopy(base)
    if isinstance(command,dict):
        op=command.get('op');ident=command.get('layer_id');touched=set()
        if op in {'split','delete','delete_range'}:touched.add(ident)
        elif op=='join_next':
            first=next((l for l in working['layers'] if l['id']==ident),None)
            if first:
                touched.add(ident)
                touched.update(l['id'] for l in working['layers'] if l['id']!=ident and l.get('track_id')==first.get('track_id') and l.get('asset_id')==first.get('asset_id') and l.get('start_sample')==first.get('end_sample'))
        elif op=='delete_track':touched.update(l['id'] for l in working['layers'] if l['track_id']==command.get('track_id'))
        detach_touching(working.get('layers', []),touched,'Source sections changed by '+str(op))
    return _apply_timeline_edit(working,command,clipboard_layer,_defer_capacity=_defer_capacity)


def _apply_timeline_edit(base, command, clipboard_layer=None, *, _defer_capacity=False):
    if not isinstance(command, dict) or command.get('op') not in {'split', 'join_next', 'delete', 'duplicate', 'paste', 'delete_range', 'delete_track', 'delete_track_group', 'delete_track_range', 'move_clips', 'delete_clips'}:
        raise ValueError('Choose split, join_next, delete, duplicate, paste or delete_range.')
    if command['op']=='delete_track_group':
        from processing_group_guard import group_tracks
        if set(command)-{'op','track_id'}:
            raise ValueError('Choose a track to delete.')
        group=group_tracks(base.get('tracks',[]),base.get('layers',[]),command.get('track_id'))
        working=copy.deepcopy(base)
        for ident in group:
            layers,tracks=apply_timeline_edit(working,{'op':'delete_track','track_id':ident,'ripple':False})
            working.update(layers=layers,tracks=tracks)
        return working['layers'],working['tracks']
    if command['op'] in {'move_clips', 'delete_clips'}:
        from timeline_selection import edit_selection
        return edit_selection(base, command)
    if command['op'] == 'delete_track_range':
        from timeline_range import delete_track_range
        return delete_track_range(base, command)
    layers, tracks = copy.deepcopy(base.get('layers', [])), copy.deepcopy(base.get('tracks', []))
    if 'ripple' in command and type(command['ripple']) is not bool:raise ValueError('Ripple must be on or off.')
    if command['op']=='delete_track':
        track_id=command.get('track_id')
        if track_id not in {track['id'] for track in tracks}:raise ValueError('Choose an existing track.')
        removed={l['id']:l for l in layers if l['track_id']==track_id}
        kept=[l for l in layers if l['id'] not in removed]
        for child in kept:
            parent=child.get('after_layer_id')
            if parent not in removed:continue
            if not command.get('ripple',False):child.update(after_layer_id=None,anchor_start_sample=child['start_sample'],gap_samples=0,overlap_samples=0);continue
            seen=set();anchor=child['start_sample']
            while parent in removed:
                if parent in seen:raise ValueError('The timing dependencies contain a cycle.')
                seen.add(parent);anchor=removed[parent]['start_sample'];parent=removed[parent].get('after_layer_id')
            child.update(after_layer_id=parent,anchor_start_sample=anchor,gap_samples=0,overlap_samples=0)
        return kept,[track for track in tracks if track['id']!=track_id]
    ident = command.get('layer_id')
    selected = [layer for layer in layers if layer.get('id') == ident] if command['op']!='paste' else ([copy.deepcopy(clipboard_layer)] if isinstance(clipboard_layer,dict) else [])
    if not isinstance(ident, str) or len(selected) != 1:
        raise ValueError('Choose an existing clip.')
    layer = selected[0]
    if 'ripple' in command and type(command['ripple']) is not bool:
        raise ValueError('Ripple must be on or off.')
    if command['op'] in {'split','delete_range'}:
        dry_end = layer['start_sample']+layer.get('dry_rendered_sample_count',layer.get('rendered_sample_count',0))
        edge = command.get('split_sample') if command['op']=='split' else command.get('end_sample')
        if layer.get('rendered_region') is not None or layer.get('section_history') or layer.get('section_effects') or layer.get('group_section_effects') or type(edge) is int and (edge>=dry_end if command['op']=='split' else edge>dry_end):
            from rendered_regions import edit_region
            layers=edit_region(layers,ident,command,tracks)
            return _checked(layers,tracks,_defer_capacity)
    followers = [item for item in layers if item.get('after_layer_id') == ident]
    op = command['op']
    if op in {'split', 'delete_range'} and layer.get('kind') == 'audio':
        # Older full-length clips can leave these bounds implicit.
        layer.setdefault('source_in_sample', 0)
        if 'source_out_sample' not in layer:
            layer['source_out_sample'] = layer['original_sample_count']
        # Render a divided source with its original processing context. Restarting
        # tempo/filters at each new section changes the sound and duplicates tails.
        layer.setdefault('processing_context', {key: layer.get(key, 0) for key in
            ('source_in_sample', 'source_out_sample', 'fade_in_samples', 'fade_out_samples')})
        context = layer['processing_context']
        interior_in = layer.get('fade_in_samples', 0) if layer['source_in_sample'] != context['source_in_sample'] else 0
        interior_out = layer.get('fade_out_samples', 0) if layer['source_out_sample'] != context['source_out_sample'] else 0
        if interior_in or interior_out:
            context.setdefault('section_envelopes', []).append({
                'source_in_sample': layer['source_in_sample'], 'source_out_sample': layer['source_out_sample'],
                'fade_in_samples': interior_in, 'fade_out_samples': interior_out})
            if interior_in:layer['fade_in_samples'] = 0
            if interior_out:layer['fade_out_samples'] = 0
    if op == 'join_next':
        from section_join import join_next
        layers = join_next(layers, layer)
    elif op=='delete_range':
        lo,hi=command.get('start_sample'),command.get('end_sample')
        dry=layer.get('dry_rendered_sample_count',layer.get('rendered_sample_count',0))
        start=layer['start_sample'];finish=start+dry
        if type(lo) is not int or type(hi) is not int or not start<=lo<hi<=finish:
            raise ValueError('Select a range inside the selected clip’s speech.')
        if lo==start and hi==finish:
            return apply_timeline_edit(base,{'op':'delete','layer_id':ident,'ripple':command.get('ripple',True)},_defer_capacity=_defer_capacity)
        begin,end=layer.get('source_in_sample',0),layer['source_out_sample']
        cut_lo=begin+round((lo-start)*(end-begin)/dry)
        cut_hi=begin+round((hi-start)*(end-begin)/dry)
        if not begin<=cut_lo<cut_hi<=end:raise ValueError('Select at least one source sample.')
        if lo==start:
            layer.update(source_in_sample=cut_hi,fade_in_samples=0)
            if not command.get('ripple',True):layer.update(after_layer_id=None,anchor_start_sample=hi,gap_samples=0,overlap_samples=0)
        elif hi==finish:
            layer.update(source_out_sample=cut_lo,fade_out_samples=0)
            if not command.get('ripple',True):
                for child in followers:child.update(after_layer_id=None,anchor_start_sample=child['start_sample'],gap_samples=0,overlap_samples=0)
        else:
            second=copy.deepcopy(layer);second.update(id=section_clip_id(layers, command),source_in_sample=cut_hi,fade_in_samples=0,after_layer_id=ident,gap_samples=0 if command.get('ripple',True) else hi-lo,overlap_samples=0)
            layer.update(source_out_sample=cut_lo,fade_out_samples=0)
            second.setdefault('slot',{})['section_id']=second['id']
            for child in followers:child['after_layer_id']=second['id']
            layers.insert(layers.index(layer)+1,second)
    elif op == 'split':
        at = command.get('split_sample')
        dry = layer.get('dry_rendered_sample_count', layer.get('rendered_sample_count', 0))
        offset = at - layer['start_sample'] if type(at) is int else -1
        if type(at) is not int or not 0 < offset < dry:
            raise ValueError('Place the playhead inside this clip’s speech, before its effect tail.')
        begin, end = layer.get('source_in_sample', 0), layer['source_out_sample']
        cut = begin + round(offset * (end - begin) / dry)
        if not begin < cut < end:
            raise ValueError('A split must leave source audio on both sides.')
        second = copy.deepcopy(layer)
        second.update(id=section_clip_id(layers, command), source_in_sample=cut,
                      fade_in_samples=0, after_layer_id=ident, gap_samples=0, overlap_samples=0)
        layer.update(source_out_sample=cut, fade_out_samples=0)
        old_label = layer.get('label') or layer.get('slot', {}).get('placeholder') or layer.get('slot', {}).get('spoken_text') or 'Clip'
        layer['label'], second['label'] = old_label + ' · 1', old_label + ' · 2'
        second.setdefault('slot', {})['section_id'] = second['id']
        layer.setdefault('slot', {})['section_id'] = layer['id']
        for child in followers:
            child['after_layer_id'] = second['id']
        layers.insert(layers.index(layer) + 1, second)
    elif op == 'delete':
        layers.remove(layer)
        for child in followers:
            if command.get('ripple', True):
                child['after_layer_id'] = layer.get('after_layer_id')
                if child['after_layer_id'] is None:
                    child['anchor_start_sample'] = layer['start_sample']
            else:
                child.update(after_layer_id=None, anchor_start_sample=child['start_sample'])
            child.update(gap_samples=0, overlap_samples=0)
    else:
        duplicate = copy.deepcopy(layer)
        duplicate.pop('guide_binding',None);duplicate.pop('guide_binding_resolution',None)
        duplicate.pop('replacement_playback_source',None);duplicate.pop('replacement_playback_candidate',None)
        track = command.get('track_id', layer['track_id'])
        if track not in {item['id'] for item in tracks}:
            raise ValueError('Choose an existing destination track.')
        duplicate.update(id='clip-' + uuid.uuid4().hex, track_id=track, after_layer_id=ident,
                         gap_samples=0, overlap_samples=0)
        duplicate.setdefault('slot', {})['section_id'] = duplicate['id']
        if op=='paste' or 'position_sample' in command:
            position=command.get('position_sample',0)
            if type(position) is not int or position<0:raise ValueError('Paste position must be a nonnegative sample.')
            duplicate.update(after_layer_id=None,anchor_start_sample=position)
        if op!='paste' and 'position_sample' not in command and command.get('ripple', True):
            for child in followers:
                child['after_layer_id'] = duplicate['id']
        layers.insert(len(layers) if op=='paste' else layers.index(layer)+1, duplicate)
    return _checked(layers,tracks,_defer_capacity)


def _checked(layers,tracks,_defer_capacity=False):
    if not _defer_capacity and len(layers) > MAX_SEGMENTS:
        raise ValueError('This arrangement exceeds the current runtime clip capacity.')
    ids = {item['id']: item for item in layers}
    track_ids = {item['id'] for item in tracks}
    if len(ids) != len(layers):
        raise ValueError('Clip identifiers must be unique.')
    from guide_bindings import validate_graph
    validate_graph(layers)
    for item in layers:
        if item['track_id'] not in track_ids:
            raise ValueError('A clip refers to an unknown track.')
    return layers, tracks
