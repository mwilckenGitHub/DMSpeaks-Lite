"""Bounded core for per-delimiter-occurrence effect inheritance.

Stage: Pre-mix candidate processing. No double application.
"""
from copy import deepcopy
from effects_schema import validate_effects

def resolve_effects(group_chain, override=None):
    """Resolve group effects with optional override.

    Args:
        group_chain: List of effect dicts.
        override: Optional dict with keys 'mode' and 'effects'.

    Returns:
        List of effective effect dicts.
    """
    normalized_group = validate_effects(group_chain)

    if override is None:
        return deepcopy(normalized_group)

    if not isinstance(override, dict):
        raise TypeError("override must be a dict")

    allowed_keys = {'mode', 'effects'}
    if set(override) - allowed_keys or 'mode' not in override:
        raise ValueError('override requires mode and permits only mode/effects')

    mode = override['mode']
    if mode not in ('inherit', 'replace', 'bypass'):
        raise ValueError(f"invalid mode: {mode}")

    effects_val = override.get('effects')

    if mode == 'inherit':
        if effects_val is not None:
            raise ValueError("inherit mode must not have effects")
        return deepcopy(normalized_group)

    elif mode == 'replace':
        if not isinstance(effects_val, list):
            raise TypeError("replace mode requires effects list")
        return validate_effects(effects_val)

    else:  # bypass
        if effects_val is not None:
            raise ValueError("bypass mode must not have effects")
        return []

def resolve_track_effects(group, track=None):
    """Resolve track effects considering group bypass state.

    Args:
        group: Dict with keys 'effects' and 'bypassed'.
        track: Optional override dict.

    Returns:
        List of effective effect dicts.
    """
    if not isinstance(group, dict):
        raise TypeError("group must be a dict")

    if set(group) - {'effects', 'bypassed'}:
        raise ValueError("group permits only effects and bypassed")

    effects = group.get('effects', [])
    bypassed = group.get('bypassed', False)

    if not isinstance(bypassed, bool):
        raise TypeError("bypassed must be a bool")

    normalized_group = validate_effects(effects)

    # Validate overrides even while a group is bypassed. A per-candidate replace
    # deliberately overrides inherited group state, including group bypass.
    return resolve_effects([] if bypassed else normalized_group, track)
