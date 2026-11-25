"""Validate edits to locked audio groups before an arrangement is rebuilt."""

from processing_group_guard import group_tracks


def validate_lock_state(tracks, layers):
    """Reject a saved group whose lock is malformed or only partly applied."""
    if not isinstance(tracks, list) or not isinstance(layers, list):
        raise ValueError('Invalid tracks or clips in the saved arrangement.')
    by_id = {}
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get('id'), str) or not track['id']:
            raise ValueError('Invalid audio track in the saved arrangement.')
        if type(track.get('group_locked', False)) is not bool:
            raise ValueError('Group lock must be on or off.')
        if track['id'] in by_id:
            raise ValueError('Track identifiers must be unique.')
        by_id[track['id']] = track
    for track in tracks:
        if track.get('group_locked') is not True:
            continue
        group = group_tracks(tracks, layers, track['id'])
        if any(by_id[ident].get('group_locked') is not True for ident in group):
            raise ValueError('The saved delimiter group has an inconsistent lock.')


def validate_transition(before_tracks, before_layers, after_tracks, after_layers):
    """Keep a locked group's clips, track properties and order unchanged.

    Lock and unlock are separate edits. A group cannot be unlocked and changed
    in the same request. This makes the rule independent of the UI controls.
    """
    before = {track['id']: track for track in before_tracks}
    after = {track['id']: track for track in after_tracks}
    if any(type(track.get('group_locked', False)) is not bool for track in after_tracks):
        raise ValueError('Group lock must be on or off.')
    visited = set()
    for track in before_tracks:
        ident = track['id']
        if ident in visited:
            continue
        if not track.get('group_locked', False) and not after.get(ident, {}).get('group_locked', False):
            continue
        group = group_tracks(before_tracks, before_layers, ident)
        visited.update(group)
        old_flags = {before[item].get('group_locked', False) for item in group}
        new_flags = {after[item].get('group_locked', False) for item in group if item in after}
        if len(old_flags) != 1 or len(new_flags) != 1 or any(item not in after for item in group):
            raise ValueError('Unlock the entire group before changing or deleting it.')
        old_positions = [next(i for i, track in enumerate(before_tracks) if track['id'] == item) for item in group]
        new_positions = [next(i for i, track in enumerate(after_tracks) if track['id'] == item) for item in group]
        def properties(track):
            return {key: value for key, value in track.items() if key != 'group_locked'}
        old_clips = [layer for layer in before_layers if layer.get('track_id') in group]
        new_clips = [layer for layer in after_layers if layer.get('track_id') in group]
        if (old_positions != new_positions or old_clips != new_clips or
                any(properties(before[item]) != properties(after[item]) for item in group)):
            raise ValueError('Unlock the group before moving or editing its tracks and clips.')


def validate_scheduled(before_tracks, before_layers, scheduled_layers):
    """Reject indirect timing changes caused by a neighboring clip edit."""
    locked = {track['id'] for track in before_tracks if track.get('group_locked') is True}
    previous = {layer['id']: layer for layer in before_layers if layer.get('track_id') in locked}
    current = {layer['id']: layer for layer in scheduled_layers if layer.get('track_id') in locked}
    if previous.keys() != current.keys():
        raise ValueError('Unlock the group before changing its clips.')
    for ident, original in previous.items():
        for key in ('start_sample', 'end_sample'):
            if original.get(key) != current[ident].get(key):
                raise ValueError('This edit would move a locked group. Unlock it first.')
