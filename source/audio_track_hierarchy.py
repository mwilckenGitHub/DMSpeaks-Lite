"""Portable hierarchy for ordinary and effect tracks.

The hierarchy stores organization only. It never contains PCM data or requests
new speech. Track IDs refer to an arrangement's existing ordered track stack.
This module validates saved hierarchy data and maintains track references.
"""

import copy
from dataclasses import dataclass

MAX_GROUPS = 256
MAX_DEPTH = 32


def update_track_references(hierarchy, before, after):
    """Retain folders and order, removing deleted tracks and appending new ones."""
    old_ids = [track['id'] for track in before]
    new_ids = [track['id'] for track in after]
    old_set, new_set = set(old_ids), set(new_ids)
    added = [ident for ident in new_ids if ident not in old_set]
    retained = [ident for ident in old_ids if ident in new_set]
    if new_ids != retained + added:
        raise ValueError('Use the folder controls to change track order.')
    result = copy.deepcopy(hierarchy)
    for children in [result['roots'], *(group['children'] for group in result['groups'])]:
        children[:] = [child for child in children if child['type'] != 'track' or child['id'] in new_set]
    result['roots'].extend({'type': 'track', 'id': ident} for ident in added)
    return result


@dataclass(frozen=True)
class GroupNode:
    """One named folder and its ordered track or child-folder references."""

    id: str
    name: str
    children: tuple[tuple[str, str], ...]
    locked: bool = False


@dataclass(frozen=True)
class TrackHierarchy:
    """Validated, immutable snapshot of an arrangement's folder structure."""

    roots: tuple[tuple[str, str], ...]
    groups: tuple[GroupNode, ...]

    def effective_locks(self):
        """Return inherited folder-lock state for every audio or effect track."""
        groups = {group.id: group for group in self.groups}
        locks = {}

        def visit(reference, inherited):
            kind, ident = reference
            if kind == 'track':
                locks[ident] = inherited
                return
            group = groups[ident]
            for child in group.children:
                visit(child, inherited or group.locked)

        for reference in self.roots:
            visit(reference, False)
        return locks

    @classmethod
    def from_mapping(cls, data, stack, *, layers=None, tracks=None):
        if not isinstance(data, dict) or set(data) != {'version', 'roots', 'groups'}:
            raise ValueError('Invalid track hierarchy.')
        if type(data['version']) is not int or data['version'] != 1:
            raise ValueError('Unsupported track hierarchy version.')
        if not isinstance(data['groups'], list) or len(data['groups']) > MAX_GROUPS:
            raise ValueError('Invalid number of track groups.')
        if not isinstance(stack, list):
            raise ValueError('Invalid track stack.')
        track_ids = [item.get('id') if isinstance(item, dict) else None for item in stack]
        if (any(not isinstance(ident, str) or not ident for ident in track_ids)
                or len(set(track_ids)) != len(track_ids)):
            raise ValueError('Track identifiers must be unique.')

        def references(items):
            if not isinstance(items, list):
                raise ValueError('Group children must be an ordered list.')
            result = []
            for item in items:
                if (not isinstance(item, dict) or set(item) != {'type', 'id'}
                        or item['type'] not in {'track', 'group'}
                        or not isinstance(item['id'], str) or not item['id']):
                    raise ValueError('Invalid hierarchy reference.')
                result.append((item['type'], item['id']))
            return tuple(result)

        groups = []
        for raw in data['groups']:
            if not isinstance(raw, dict) or set(raw) != {'id', 'name', 'children', 'locked'}:
                raise ValueError('Invalid track group.')
            if (not isinstance(raw['id'], str) or not raw['id']
                    or not isinstance(raw['name'], str) or not raw['name'].strip()
                    or type(raw['locked']) is not bool):
                raise ValueError('Each group needs an identity, name and lock state.')
            groups.append(GroupNode(raw['id'], raw['name'], references(raw['children']), raw['locked']))
        by_id = {group.id: group for group in groups}
        if len(by_id) != len(groups):
            raise ValueError('Group identifiers must be unique.')

        roots = references(data['roots'])
        track_id_set = set(track_ids)
        seen_groups, seen_tracks, flattened = set(), set(), []
        track_parents = {}

        def visit(ref, depth, parent):
            kind, ident = ref
            if kind == 'track':
                if ident not in track_id_set or ident in seen_tracks:
                    raise ValueError('Every track must occur exactly once in the hierarchy.')
                seen_tracks.add(ident)
                flattened.append(ident)
                track_parents[ident] = parent
                return
            if depth > MAX_DEPTH:
                raise ValueError('Track groups are nested too deeply.')
            if ident not in by_id or ident in seen_groups:
                raise ValueError('Each group must have one parent and cannot form a cycle.')
            seen_groups.add(ident)
            for child in by_id[ident].children:
                visit(child, depth + 1, ident)

        for ref in roots:
            visit(ref, 1, None)
        if seen_groups != set(by_id) or flattened != track_ids:
            raise ValueError('The hierarchy must contain every group and match track order.')
        if layers is not None and tracks is not None:
            from processing_group_guard import membership
            linked = {}
            for ident, info in membership(tracks, layers).items():
                for group_id in info['groups']:
                    linked.setdefault(group_id, []).append(ident)
            for members in linked.values():
                parents = {track_parents[ident] for ident in members}
                if None in parents or len(parents) != 1:
                    raise ValueError('A delimiter and its tracks must remain in one group.')
        return cls(roots, tuple(groups))
