"""Per-occurrence authoring schema. Local Qwen draft inspected before integration."""
from copy import deepcopy
import re
from effects_schema import validate_effects
from group_effects import resolve_track_effects

def validate_group(raw):
    if not isinstance(raw, dict):
        raise ValueError("raw must be a dict")

    required = {'id', 'slot_order', 'source_revision', 'source_start', 'source_end', 'placeholder'}
    optional = {'color_mode', 'color', 'effects', 'bypassed', 'candidate_overrides', 'section_effects'}
    allowed = required | optional

    for key in raw:
        if key not in allowed:
            raise ValueError(f"Unknown key: {key}")

    for key in required:
        if key not in raw:
            raise ValueError(f"Missing required key: {key}")

    def _check_str(val, max_len, field_name):
        if not isinstance(val, str) or len(val) == 0 or len(val) > max_len:
            raise ValueError(f"{field_name} must be nonempty str <= {max_len}")
        return val

    def _check_int(val, field_name, non_negative=False):
        if isinstance(val, bool) or not isinstance(val, int):
            raise ValueError(f"{field_name} must be int, not bool")
        if non_negative and val < 0:
            raise ValueError(f"{field_name} must be nonnegative")
        return val

    def _check_bool(val, field_name):
        if not isinstance(val, bool):
            raise ValueError(f"{field_name} must be bool")
        return val

    def _check_color(val):
        if val is None:
            return None
        if not isinstance(val, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', val):
            raise ValueError("color must be None or #RRGGBB")
        return val.upper()

    def _check_effects(val):
        if not isinstance(val, list):
            raise ValueError("effects must be a list")
        return validate_effects(val)

    def _check_override(asset_id, override):
        if not isinstance(asset_id, str) or len(asset_id) == 0 or len(asset_id) > 128:
            raise ValueError("asset_id must be nonempty str <= 128")
        if not isinstance(override, dict):
            raise ValueError("override must be a dict")

        override_required = {'mode'}
        override_optional = {'effects', 'section_effects'}
        override_allowed = override_required | override_optional

        for key in override:
            if key not in override_allowed:
                raise ValueError(f"Unknown override key: {key}")

        if 'mode' not in override:
            raise ValueError("Missing required override key: mode")

        mode = override['mode']
        if mode not in ('inherit', 'replace', 'bypass'):
            raise ValueError("mode must be inherit, replace, or bypass")

        result = {'mode': mode}

        if mode in ('inherit', 'bypass'):
            if 'effects' in override and override['effects'] is not None:
                raise ValueError("effects must be absent or None for inherit/bypass")
            if override.get('section_effects') is not None:
                raise ValueError('section_effects must be absent for inherit/bypass')
        else:  # replace
            if 'effects' not in override:
                raise ValueError("effects required for replace mode")
            result['effects'] = _check_effects(override['effects'])
            if 'section_effects' in override:result['section_effects'] = _check_effects(override['section_effects'])

        return result

    out = {}

    out['id'] = _check_str(raw['id'], 128, 'id')
    out['source_revision'] = _check_str(raw['source_revision'], 128, 'source_revision')
    out['placeholder'] = _check_str(raw['placeholder'], 512, 'placeholder')

    out['slot_order'] = _check_int(raw['slot_order'], 'slot_order')
    if out['slot_order'] <= 0:
        raise ValueError("slot_order must be positive")

    out['source_start'] = _check_int(raw['source_start'], 'source_start', non_negative=True)
    out['source_end'] = _check_int(raw['source_end'], 'source_end', non_negative=True)
    if out['source_start'] >= out['source_end']:
        raise ValueError("source_start must be < source_end")

    color_mode = raw.get('color_mode', 'theme')
    if color_mode not in ('theme', 'auto', 'manual'):
        raise ValueError("color_mode must be theme, auto, or manual")
    out['color_mode'] = color_mode

    color = raw.get('color', None)
    out['color'] = _check_color(color)

    if color_mode == 'manual' and out['color'] is None:
        raise ValueError("manual color_mode requires color")

    effects = raw.get('effects', [])
    out['effects'] = _check_effects(effects)
    out['section_effects'] = _check_effects(raw.get('section_effects', []))

    bypassed = raw.get('bypassed', False)
    out['bypassed'] = _check_bool(bypassed, 'bypassed')

    candidate_overrides = raw.get('candidate_overrides', {})
    if not isinstance(candidate_overrides, dict):
        raise ValueError("candidate_overrides must be a dict")

    out['candidate_overrides'] = {}
    for asset_id, override in candidate_overrides.items():
        out['candidate_overrides'][asset_id] = _check_override(asset_id, override)

    return out

def effective_effects(raw, asset_id):
    group = validate_group(raw)
    try:
        return resolve_track_effects(
            {'effects': group['effects'], 'bypassed': group['bypassed']},
            {key:value for key,value in group['candidate_overrides'][asset_id].items() if key!='section_effects'} if asset_id in group['candidate_overrides'] else None
        )
    except TypeError as e:
        raise ValueError(str(e)) from e


def effective_section_effects(raw, asset_id):
    group=validate_group(raw)
    override=group['candidate_overrides'].get(asset_id,{})
    if override.get('mode')=='bypass':return []
    if override.get('mode')=='replace' and 'section_effects' in override:
        return deepcopy(override['section_effects'])
    return [] if group['bypassed'] else deepcopy(group['section_effects'])
