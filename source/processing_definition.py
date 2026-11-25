"""Validate a portable description of experimental scoped processing layers.

The returned definition contains only authored settings, never decoded audio.
Validation is shared by the audition and any future arrangement integration.
"""
import copy

from processing_scope import plan
from processing_windows import apply

MAX_PROCESSORS = 8
MAX_PROCESSING_CLIPS = 32


def require_ordinary_rebuild(asset, *, allow_track_hierarchy=False):
    """Refuse an older rebuild path that would discard scoped processing."""
    if asset.get('processing_definition') is not None:
        raise ValueError('This operation does not yet support scoped effect layers. The saved effects have not been changed.')
    if asset.get('track_hierarchy') is not None and not allow_track_hierarchy:
        raise ValueError('This operation does not yet support explicit track groups. The saved arrangement has not changed.')


def validate(tracks, stack, duration_samples, *, layers=None):
    if not isinstance(tracks, list) or any(not isinstance(track, dict) for track in tracks):
        raise ValueError('Choose valid arrangement tracks.')
    if not isinstance(stack, list) or any(not isinstance(item, dict) for item in stack):
        raise ValueError('Choose a valid processing stack.')
    if any(set(item) != ({'id', 'processing'} if 'processing' in item else {'id'}) for item in stack):
        raise ValueError('Only processing settings may be changed in this preview.')
    base_ids = [track.get('id') for track in tracks]
    ordinary_ids = [item.get('id') for item in stack if 'processing' not in item]
    if ordinary_ids != base_ids:
        raise ValueError('Keep the arrangement tracks in their existing order for this preview.')
    processors = [item for item in stack if 'processing' in item]
    if len(processors) > MAX_PROCESSORS:
        raise ValueError('This preview supports up to eight processing tracks.')
    # Validate IDs and duration even if no processing layers are present.
    if not stack:
        raise ValueError('Choose at least one track.')
    plan(stack, stack[0].get('id'), duration_samples, clips=[])
    for item in processors:
        spec = item['processing']
        if not isinstance(spec, dict) or set(spec) - {'direction', 'clips', 'effects', 'strength', 'fade_samples'}:
            raise ValueError('Invalid processing track settings.')
        clips = spec.get('clips')
        if isinstance(clips, list) and len(clips) > MAX_PROCESSING_CLIPS:
            raise ValueError('This preview supports up to 32 clips per processing track.')
        plan(stack, item['id'], duration_samples, direction=spec.get('direction', 'above'), clips=clips)
        apply([], 1, [], spec.get('effects', []), strength=spec.get('strength', 1),
              fade_samples=spec.get('fade_samples', 220))
    if layers is not None:
        from processing_group_guard import validate as validate_groups
        validate_groups(stack, tracks, layers)
    return copy.deepcopy(stack)
