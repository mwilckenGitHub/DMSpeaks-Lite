"""Apply a time selection to explicit tracks as one immutable arrangement edit.

Ripple follows the arrangement's dependency links, including links across tracks.
Unlinked clips retain their anchors. This is not a destructive global time cut.
"""
import copy
from engine_limits import MAX_SEGMENTS
from layer_schedule import schedule_layers
from guide_bindings import parent_id


def range_scope(base, command):
    if not isinstance(command,dict):raise ValueError('Choose a time selection.')
    lo, hi = command.get('start_sample'), command.get('end_sample')
    if type(lo) is not int or type(hi) is not int or not 0 <= lo < hi <= base.get('sample_count',0):
        raise ValueError('Set a time range inside the arrangement.')
    if base.get('kind')=='scoped-arrangement-preview' and hi > base.get('dry_sample_count',0):
        raise ValueError('Set a range inside the editable tracks, before the effect tail.')
    tracks = {track['id'] for track in base.get('tracks',[])}
    if 'layer_ids' in command:
        selected=command['layer_ids'];known={layer['id'] for layer in base.get('layers',[])}
        if (not isinstance(selected,list) or not selected or any(not isinstance(i,str) or i not in known for i in selected) or len(set(selected))!=len(selected) or 'track_ids' in command):
            raise ValueError('Choose unique existing clips for the time selection.')
        layers=[layer for layer in base['layers'] if layer['id'] in selected]
        scope={layer['track_id'] for layer in layers}
    else:
        selected=command.get('track_ids')
        if not isinstance(selected,list) or not selected or any(not isinstance(t,str) or t not in tracks for t in selected) or len(set(selected))!=len(selected):
            raise ValueError('Choose existing tracks for the time selection.')
        scope=set(selected);layers=[layer for layer in base['layers'] if layer['track_id'] in scope]
    ordered=[track['id'] for track in base['tracks'] if track['id'] in scope]
    if not ordered:raise ValueError('Choose an existing track for the time selection.')
    return lo,hi,layers,ordered


def delete_track_range(base, command):
    from timeline_commands import apply_timeline_edit
    lo,hi,selected_layers,_=range_scope(base,command)
    selected={layer['id'] for layer in selected_layers}
    ripple=command.get('ripple',True)
    if type(ripple) is not bool:raise ValueError('Ripple must be on or off.')
    # Reject invalid dependencies before any edits, including cycles that a
    # deletion could otherwise hide. The caller's immutable base stays intact.
    schedule_layers(base.get('layers', []))
    originals = {layer['id']: layer for layer in base['layers']}
    order, visited = [], set()
    def visit(ident):
        if ident in visited:return
        parent = parent_id(originals[ident])
        if parent is not None:visit(parent)
        visited.add(ident);order.append(ident)
    for ident in originals:visit(ident)
    planned = {}
    for ident, layer in originals.items():
        start, end = layer['start_sample'], layer['end_sample']
        if ident not in selected or start >= hi or end <= lo:continue
        if lo <= start and hi >= end:
            planned[ident] = {'op':'delete', 'layer_id':ident, 'ripple':ripple}
            continue
        planned[ident] = {'op':'delete_range', 'layer_id':ident, 'start_sample':max(lo,start), 'end_sample':min(hi,end), 'ripple':ripple}
    if not planned:
        raise ValueError('No clips intersect the selection on these tracks.')
    working = copy.deepcopy(base)
    # Descendants first: original absolute timestamps remain valid while earlier
    # source edits rewire the already-edited descendants, even across tracks.
    for ident in reversed(order):
        if ident in planned:
            layers, updated_tracks = apply_timeline_edit(working, planned[ident], _defer_capacity=True)
            working.update(layers=layers, tracks=updated_tracks)
    if len(working['layers']) > MAX_SEGMENTS:
        raise ValueError('This arrangement exceeds the current runtime clip capacity.')
    return working['layers'], working['tracks']
