"""Explicit in-track replacement mixes; delivery remains the original by default."""
import copy,hashlib
from layered_audio import render_layers
from replacement_playback_state import references
from guide_bindings import binding,guide,parent_id


def source_asset(store, asset):
    if not references(asset):return asset
    original=store.asset(asset['replacement_playback']['base_asset_id'])
    if original.get('replacement_playback') is not None:raise ValueError('Replacement preview base must be an original arrangement.')
    return original


def delivery_asset(store, asset):
    if not references(asset):return asset
    return source_asset(store,asset) if not asset['replacement_playback']['include_in_export'] else asset


def update_tracks(app, preview, data, status):
    """Apply ordinary track controls without committing checked alternatives.

    Keep a revised single-delivery base as well as the checked audition. Rebuild
    its dependencies because track processing can change candidate durations.
    Clip edits require an explicit return to the selected recording first.
    """
    if data.get('operation') is not None or data.get('layers') != preview['layers']:
        raise ValueError('Return to the selected replacement before editing clip positions or source ranges.')
    original=source_asset(app.store,preview)
    tracks=data.get('tracks',preview['tracks'])
    if tracks==preview['tracks']:return preview
    revised=app._layers_render({'asset_id':original['id'],'layers':original['layers'],'tracks':tracks},status)
    return rebuild(app,preview,revised,status)


def rebuild(app, preview, revised, status):
    """Retain checked choices around an immutable revised delivery arrangement."""
    policy=preview['replacement_playback']
    mixed=app._layers_render({'asset_id':revised['id'],'operation':{
        'op':'replacement_playback','selections':copy.deepcopy(policy['selections']),
        'include_in_export':policy['include_in_export']}},status)
    if policy.get('selected_has_effects'):
        # Audio Lab uses this reference to find the dry graph for its wet mix.
        return mixed
    mixed['parent_assembly_id']=preview['id']
    return app.store.add_asset(mixed)


def independent_layers(store, preview, layers):
    """Merge independent-track edits onto the single-delivery graph.

    Preview followers may point to a longer checked alternative. Preserve the
    original dependencies for unchanged tracks; edited/new tracks resolve any
    alternative reference back to its occurrence before rebuilding the preview.
    """
    roles={'background_music','background_ambience','imported_audio'}
    independent=lambda layer:layer.get('clip_role') in roles
    if [l for l in layers if not independent(l)] != [l for l in preview['layers'] if not independent(l)]:
        raise ValueError('Return to the selected replacement before editing speech clips.')
    original=source_asset(store,preview)
    shown={l['id']:l for l in preview['layers']}
    requested={l['id']:l for l in layers if independent(l)}
    roots={l['id']:l['replacement_playback_source'] for l in preview['layers'] if l.get('replacement_playback_source')}
    def revised(layer):
        result=copy.deepcopy(layer)
        if result.get('after_layer_id') in roots:result['after_layer_id']=roots[result['after_layer_id']]
        if binding(result) and result['guide_binding']['target_layer_id'] in roots:
            result['guide_binding']['target_layer_id']=roots[result['guide_binding']['target_layer_id']]
        return result
    merged=[];existing=set()
    for layer in original['layers']:
        existing.add(layer['id'])
        if not independent(layer):merged.append(copy.deepcopy(layer))
        elif layer['id'] in requested:
            edited=requested[layer['id']]
            merged.append(copy.deepcopy(layer) if edited==shown.get(layer['id']) else revised(edited))
    merged.extend(revised(layer) for ident,layer in requested.items() if ident not in existing)
    return original,merged


def build(app, base, operation):
    from track_candidates import replace
    original=source_asset(app.store,base)
    selections=operation.get('selections',{})
    include=operation.get('include_in_export',False)
    if type(include) is not bool or not isinstance(selections,dict) or len(selections)>64:
        raise ValueError('Choose replacement playback settings.')
    from contextual_choices import FORMAT
    if (original.get('contextual_assembly') or {}).get('format') == FORMAT:
        return _contextual_stack(app, original, selections, include)
    tracks=copy.deepcopy(original['tracks']);layers=copy.deepcopy(original['layers'])
    normalized={};longest_by_occurrence={}
    original_layers={layer['id']:layer for layer in original['layers']}
    stacked_ids={key for key,ids in selections.items() if isinstance(ids,list) and len(ids)>1}
    for layer_id,ids in selections.items():
        if not isinstance(layer_id,str) or not isinstance(ids,list) or not 1<=len(ids)<=64 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):
            raise ValueError('Check 1–64 distinct recordings per replacement occurrence.')
        selected=original_layers.get(layer_id)
        if not selected or selected.get('slot',{}).get('type') in (None,'fixed'):
            raise ValueError('Choose an existing replacement occurrence.')
        alternatives=[];stacked=len(ids)>1
        for index,ident in enumerate(ids):
            # Always validate candidate/review compatibility, including the current choice.
            changed,_=replace(app,original,dict(layer_id=layer_id,candidate_id=ident))
            candidate=copy.deepcopy(selected) if ident==selected.get('asset_id') else next(l for l in changed if l['id']==layer_id)
            candidate['id']=layer_id if index==0 else layer_id+'--replacement-'+hashlib.sha256(ident.encode()).hexdigest()[:16]
            candidate['replacement_playback_source']=layer_id
            candidate['replacement_playback_candidate']=ident
            candidate['overlap_samples']=0
            candidate.pop('replacement_snap', None)
            if stacked:
                measurement={**candidate,'guide_binding':None,'after_layer_id':None,
                             'anchor_start_sample':0,'gap_samples':0}
                _,info=render_layers([measurement],tracks,app.store,audition_only=original.get('audition_only',False))
                measured=info['layers'][0]
                candidate.update(rendered_sample_count=measured['rendered_sample_count'],snap_points=measured['snap_points'])
            alternatives.append(candidate)
        if stacked:
            ends=[clip['rendered_sample_count'] for clip in alternatives]
            link=binding(selected)
            if link and parent_id(selected) not in stacked_ids:
                try:
                    ends=[end-guide(clip,link['own_guide_id'])['sample'] for clip,end in zip(alternatives,ends)]
                except ValueError:
                    # Incomplete guide coverage uses the measured start of the selected clip.
                    for clip in alternatives:
                        clip.update(guide_binding=None,after_layer_id=None,
                                    anchor_start_sample=selected['start_sample'],gap_samples=0)
            latest=max(zip(ends,alternatives),key=lambda item:item[0])[1]
            longest_by_occurrence[layer_id]=latest['id']
        rebuilt=[]
        for layer in layers:
            if layer['id']==layer_id:rebuilt.extend(alternatives)
            else:rebuilt.append(layer)
        layers=rebuilt;normalized[layer_id]=ids
    # Use the saved graph to reconnect every follower, including another stack.
    for layer in layers:
        occurrence=layer.get('replacement_playback_source')
        authored=original_layers[occurrence] if occurrence in longest_by_occurrence else layer
        latest=longest_by_occurrence.get(parent_id(authored))
        if latest is not None:
            layer.update(guide_binding=None,after_layer_id=latest,gap_samples=0,overlap_samples=0)
            layer.pop('replacement_snap',None)
    policy=dict(base_asset_id=original['id'],selections=normalized,include_in_export=include)
    if original.get('kind')=='scoped-arrangement-preview':
        policy['selected_has_effects']=True
    return original,layers,tracks,policy


def _contextual_stack(app, original, selections, include):
    from clip_comparison import MAX_CHOICES
    from dialogue_stack_preview import saved_occurrences, stack_layout
    from layered_audio import LayerSourceCache

    if include:
        raise ValueError('Use the selected recordings for contextual delivery.')
    policy = dict(base_asset_id=original['id'], selections=selections,
                  include_in_export=False, contextual=True)
    if not selections:
        return original, original['layers'], original['tracks'], policy
    layers_by_id, occurrences = saved_occurrences(app, original)
    checked = {}
    for root, ids in selections.items():
        group = occurrences.get(root)
        if (not group or not isinstance(ids, list) or not 2 <= len(ids) <= MAX_CHOICES
                or any(not isinstance(ident, str) or ident not in group['choices'] for ident in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError(f'Check 2–{MAX_CHOICES} distinct recordings per contextual occurrence.')
        checked[root] = {**group, 'choices': {ident: group['choices'][ident] for ident in ids}}
    layers, members = stack_layout(app, original, LayerSourceCache(), layers_by_id, checked)
    by_id = {layer['id']: layer for layer in layers}
    for root, ids in selections.items():
        for layer_id, ident in zip(members[root], ids):
            by_id[layer_id].update(replacement_playback_source=root, replacement_playback_candidate=ident)
    return original, layers, original['tracks'], policy
