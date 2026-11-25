"""Keep inferred delimiter track groups together across effect-layer placement."""

from audio_track import AudioTrack
from audio_track_group import AudioTrackGroup


def membership(tracks, layers):
    if not isinstance(layers, list):
        raise ValueError('Choose valid arrangement clips.')
    identities = [AudioTrack.from_mapping(track) for track in tracks]
    if len({track.id for track in identities}) != len(identities):
        raise ValueError('Track identifiers must be unique.')
    members = {track.id: {'groups':set(), 'other':False} for track in identities}
    explicit, shared = {}, {}
    for layer in layers:
        if not isinstance(layer,dict):
            raise ValueError('Choose valid arrangement clips.')
        slot, group = layer.get('slot_order'), layer.get('replacement_group_id')
        if group and type(slot) is int:
            if slot in explicit and explicit[slot] != group:
                raise ValueError('The delimiter group links are ambiguous.')
            explicit[slot] = group
        metadata = layer.get('slot') or {}
        if not isinstance(metadata, dict):
            raise ValueError('Invalid clip metadata.')
        kind = metadata.get('type')
        if (not layer.get('clip_role') and type(slot) is int and isinstance(kind, str)
                and kind not in ('', 'fixed') and layer.get('track_id') == 'delimiter:' + kind):
            shared[slot] = 'delimiter:' + kind
    for layer in layers:
        item = members.get(layer.get('track_id'))
        if item is None:
            raise ValueError('A clip references a missing track.')
        slot = layer.get('slot') or {}
        if not isinstance(slot,dict):
            raise ValueError('Invalid clip metadata.')
        order, group = layer.get('slot_order'), layer.get('replacement_group_id')
        if not layer.get('clip_role') and slot.get('type') and slot['type'] != 'fixed' and type(order) is int:
            item['groups'].add(shared.get(order) or explicit.get(order) or 'slot:'+str(order))
        elif group and not layer.get('clip_role'):
            item['groups'].add(shared.get(order) or group)
        else:
            item['other'] = True
    return members


def group_tracks(tracks, layers, track_id):
    """Resolve a complete, unambiguous group; ordinary tracks stand alone."""
    members = membership(tracks, layers)
    if track_id not in members:
        raise ValueError('The selected track is no longer available.')
    selected=members[track_id]
    if len(selected['groups'])>1 or selected['groups'] and selected['other']:
        raise ValueError('This track contains mixed delimiter groups or other audio.')
    if not selected['groups']:
        return [track_id]
    group=next(iter(selected['groups']))
    result=[track['id'] for track in tracks if group in members[track['id']]['groups']]
    if any(len(members[ident]['groups'])!=1 or members[ident]['other'] for ident in result):
        raise ValueError('A delimiter group track contains other audio or groups.')
    positions=[index for index,track in enumerate(tracks) if track['id'] in result]
    if positions[-1]-positions[0]+1!=len(positions):
        raise ValueError('The delimiter group tracks are separated.')
    return list(AudioTrackGroup(group, tuple(result)).track_ids)


def validate(stack, tracks, layers):
    if not any(item.get('processing') is not None for item in stack):
        return
    members = membership(tracks, layers)
    positions = {item['id']:index for index,item in enumerate(stack)}
    groups = {}
    for track in tracks:
        item = members[track['id']]
        if len(item['groups']) > 1 or item['groups'] and item['other']:
            raise ValueError('A delimiter track contains mixed groups or other audio. Separate it before ordering effects.')
        for group in item['groups']:
            groups.setdefault(group,[]).append(positions[track['id']])
    for indices in groups.values():
        if indices[-1]-indices[0]+1 != len(indices):
            raise ValueError('An effect layer cannot split a delimiter track group.')
