"""Atomic edits of a clip selection; reviewed local Qwen ancestry outline.

Source ranges and audio remain untouched. Translations preserve every dependency
below a selected root, including paths through unselected clips.
"""
import copy
from engine_limits import MAX_AUDIO_SECONDS
from layer_schedule import schedule_layers
from guide_bindings import parent_id,detach


def edit_selection(base, command):
    scheduled = schedule_layers(base.get('layers', []))
    by_id = {layer['id']: layer for layer in scheduled}
    selected = command.get('layer_ids')
    if (not isinstance(selected, list) or not selected or
            any(not isinstance(ident, str) or ident not in by_id for ident in selected) or
            len(set(selected)) != len(selected)):
        raise ValueError('Choose unique existing clips.')
    selected = set(selected)
    working = copy.deepcopy(base)
    if command['op'] == 'delete_clips':
        from timeline_commands import apply_timeline_edit
        ripple = command.get('ripple', True)
        if type(ripple) is not bool:raise ValueError('Ripple must be on or off.')
        # Descendants first keeps the original absolute positions valid when
        # deleting several ancestors and descendants in one history entry.
        order, seen = [], set()
        def visit(ident):
            if ident in seen:return
            parent = parent_id(by_id[ident])
            if parent is not None:visit(parent)
            seen.add(ident);order.append(ident)
        for ident in by_id:visit(ident)
        for ident in reversed(order):
            if ident in selected:
                layers, tracks = apply_timeline_edit(working, {'op':'delete', 'layer_id':ident, 'ripple':ripple})
                working.update(layers=layers, tracks=tracks)
        return working['layers'], working['tracks']
    delta = command.get('delta_samples')
    if type(delta) is not int:raise ValueError('Move distance must be an integer sample count.')
    if delta == 0:return working['layers'], working['tracks']
    roots = set()
    for ident in selected:
        parent = parent_id(by_id[ident])
        while parent is not None and parent not in selected:
            parent = parent_id(by_id[parent])
        if parent is None:roots.add(ident)
    for layer in working['layers']:
        if layer['id'] in roots:
            start = by_id[layer['id']]['start_sample'] + delta
            if start < 0:raise ValueError('The selection cannot move before the start of the timeline.')
            detach(layer,'Moved selected root away from its external timing link',start=start)
            layer.update(after_layer_id=None, anchor_start_sample=start, gap_samples=0, overlap_samples=0)
    result = schedule_layers(working['layers'])
    if any(layer['end_sample'] > 44100*MAX_AUDIO_SECONDS for layer in result):
        raise ValueError('The moved selection exceeds the current 30-minute preview capacity.')
    return result, working['tracks']
