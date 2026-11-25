"""Portable identity and validation for main-timeline replacement preview state."""

def references(asset):
    policy=asset.get('replacement_playback')
    layers=asset.get('layers',[])
    if not isinstance(layers,list) or any(not isinstance(layer,dict) for layer in layers):
        raise ValueError('Invalid replacement preview layers.')
    tagged=[layer for layer in layers if 'replacement_playback_source' in layer or 'replacement_playback_candidate' in layer]
    if policy is None:
        if tagged:raise ValueError('Replacement preview layers require their playback settings.')
        return []
    if not isinstance(policy,dict):raise ValueError('Invalid replacement playback settings.')
    base=policy.get('base_asset_id');selections=policy.get('selections')
    if not isinstance(base,str) or not base or base==asset.get('id'):
        raise ValueError('Invalid replacement preview base asset.')
    if type(policy.get('include_in_export')) is not bool:
        raise ValueError('Replacement export policy must be a boolean.')
    contextual=policy.get('contextual') is True
    if 'contextual' in policy and (not contextual or policy['include_in_export'] or policy.get('selected_has_effects')):
        raise ValueError('Invalid contextual replacement preview policy.')
    if 'selected_has_effects' in policy and policy['selected_has_effects'] is not True:
        raise ValueError('Invalid selected replacement effects policy.')
    if not isinstance(selections,dict) or not 1<=len(selections)<=64:
        raise ValueError('Choose 1–64 replacement occurrences.')
    result=[base]
    for source,candidates in selections.items():
        if not isinstance(source,str) or not source or not isinstance(candidates,list) or not 1<=len(candidates)<=64 or any(not isinstance(i,str) or not i for i in candidates) or len(set(candidates))!=len(candidates):
            raise ValueError('Choose 1–64 distinct recordings per replacement occurrence.')
        if not contextual:result.extend(candidates)
    seen={source:set() for source in selections}
    for layer in tagged:
        source=layer.get('replacement_playback_source');candidate=layer.get('replacement_playback_candidate')
        if (not isinstance(source,str) or source not in selections or not isinstance(candidate,str)
                or candidate not in selections[source] or candidate in seen[source]
                or not contextual and layer.get('asset_id')!=candidate):
            raise ValueError('Replacement preview layer does not match its playback settings.')
        if contextual:
            if not isinstance(layer.get('asset_id'),str) or not layer['asset_id']:
                raise ValueError('Contextual preview layer needs its saved recording.')
            result.append(layer['asset_id'])
        seen[source].add(candidate)
    if any(seen[source]!=set(candidates) for source,candidates in selections.items()):
        raise ValueError('Replacement preview is missing a selected recording.')
    return result


def validate(asset,assets):
    refs=references(asset)
    if not refs:return
    if any(ident not in assets for ident in refs):raise ValueError('Replacement preview asset dependency not found.')
    base=assets[refs[0]]
    if base.get('replacement_playback') is not None:raise ValueError('Replacement previews must reference an original arrangement.')
    if asset['replacement_playback'].get('selected_has_effects'):
        if base.get('kind')!='scoped-arrangement-preview' or asset.get('kind') not in {
                'scoped-arrangement-preview','inspection-preview'}:
            raise ValueError('The selected replacement needs its Audio Lab arrangement.')
        if asset.get('kind')=='scoped-arrangement-preview' and any(
                asset.get(key)!=base.get(key) for key in ('tracks','processing_definition','track_hierarchy')):
            raise ValueError('The checked replacements and selected arrangement must share tracks, folders and effects.')
    base_layers=base.get('layers')
    if not isinstance(base_layers,list) or any(not isinstance(layer,dict) for layer in base_layers):raise ValueError('Replacement preview base must be an arrangement.')
    by_id={}
    for layer in base_layers:
        ident=layer.get('id')
        if not isinstance(ident,str) or not ident or ident in by_id:raise ValueError('Invalid replacement base layer identity.')
        by_id[ident]=layer
    for source in asset['replacement_playback']['selections']:
        original=by_id.get(source)
        if not original or not isinstance(original.get('slot'),dict) or original['slot'].get('type') in (None,'fixed'):
            raise ValueError('Replacement preview occurrence not found in its base arrangement.')
        first=asset['replacement_playback']['selections'][source][0]
        if not any(layer.get('id')==source and layer.get('replacement_playback_candidate')==first for layer in asset['layers']):raise ValueError('Replacement preview is missing its primary occurrence layer.')
        if any(layer.get('track_id')!=original.get('track_id') for layer in asset['layers'] if layer.get('replacement_playback_source')==source):
            raise ValueError('Replacement preview occurrence changed its track identity.')
        if asset['replacement_playback'].get('contextual'):
            from clip_comparison import MAX_CHOICES
            from contextual_packet_layout import packet_for_layer
            packet=packet_for_layer(base,source)
            group=(base.get('contextual_choices') or {}).get(packet['region_id']) if packet else None
            candidates=asset['replacement_playback']['selections'][source]
            if not group or not 2<=len(candidates)<=MAX_CHOICES:
                raise ValueError('Contextual preview needs its original saved occurrence.')
            for layer in asset['layers']:
                if layer.get('replacement_playback_source')!=source:continue
                choice=group['choices'].get(layer['replacement_playback_candidate'])
                if (not choice or layer['asset_id']!=choice['asset_id'] or
                        [layer['source_in_sample'],layer['source_out_sample']]!=choice['word_bounds']):
                    raise ValueError('Contextual preview layer differs from its saved recording.')
    for field in ['record_id','source_revision','source_metadata_revision']:
        if base.get(field)!=asset.get(field):raise ValueError('Replacement preview and base must belong to the same source revision.')


def remap(asset,ids):
    """Only media references change; layer/track identities stay local and stable."""
    if not references(asset):return
    policy=asset['replacement_playback'];policy['base_asset_id']=ids[policy['base_asset_id']]
    if policy.get('contextual'):return
    policy['selections']={source:[ids[candidate] for candidate in candidates] for source,candidates in policy['selections'].items()}
    for layer in asset['layers']:
        if 'replacement_playback_candidate' in layer:layer['replacement_playback_candidate']=ids[layer['replacement_playback_candidate']]
