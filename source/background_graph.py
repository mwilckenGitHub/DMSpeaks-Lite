"""Pure background-track graph edits, drafted locally by Qwen and verified by the parent."""
import copy
import math

def _validate_base(base):
    if not isinstance(base, dict) or 'layers' not in base or 'tracks' not in base:
        raise ValueError("Invalid base structure")
    if not isinstance(base['layers'], list) or not base['layers'] or not isinstance(base['tracks'], list):
        raise ValueError("Invalid base lists")

def _validate_source(source):
    if not isinstance(source, dict):
        raise ValueError("Invalid source")
    for k in ('id', 'sample_count', 'channels'):
        if k not in source:
            raise ValueError(f"Missing source key: {k}")
    if not isinstance(source['id'], str) or not source['id']:
        raise ValueError('Invalid source ID')
    sc = source['sample_count']
    if isinstance(sc, bool) or not isinstance(sc, int) or sc < 1 or sc > 44100 * 1800:
        raise ValueError("Invalid sample_count")
    ch = source['channels']
    if isinstance(ch, bool) or not isinstance(ch, int) or ch not in (1, 2):
        raise ValueError("Invalid channels")

def _validate_common(role, name, ident, anchor_sample, gain_db):
    if role not in ('music', 'ambience'):
        raise ValueError("Invalid role")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("Invalid name")
    if not isinstance(ident, str) or not ident:
        raise ValueError("Invalid ident")
    if isinstance(anchor_sample, bool) or not isinstance(anchor_sample, int) or anchor_sample < 0 or anchor_sample > 44100 * 1800:
        raise ValueError("Invalid anchor_sample")
    if isinstance(gain_db, bool) or not isinstance(gain_db, (int, float)) or not math.isfinite(gain_db) or gain_db < -60 or gain_db > 12:
        raise ValueError("Invalid gain_db")

def add_layer(base, source, role, name, ident, anchor_sample=0, gain_db=-18):
    _validate_base(base)
    _validate_source(source)
    _validate_common(role, name, ident, anchor_sample, gain_db)

    existing_ids = {l.get('id') for l in base['layers']}
    if ident in existing_ids or 'background:'+ident in {t.get('id') for t in base['tracks']}:
        raise ValueError("Layer ID already exists")

    layers = copy.deepcopy(base['layers'])
    tracks = copy.deepcopy(base['tracks'])

    orders = [l.get('slot_order', 0) for l in layers]
    if any(type(order) is not int or order < 0 for order in orders):
        raise ValueError('Invalid existing slot order')
    max_slot = max(orders, default=0)
    slot_order = max_slot + 1
    track_id = f"background:{ident}"
    clip_role = f"background_{role}"
    count = source['sample_count']

    new_layer = {
        'id': ident,
        'track_id': track_id,
        'clip_role': clip_role,
        'kind': 'audio',
        'slot_order': slot_order,
        'asset_id': source['id'],
        'after_layer_id': None,
        'anchor_start_sample': anchor_sample,
        'original_sample_count': count,
        'original_sample_rate': 44100,
        'original_seconds': count / 44100,
        'source_in_sample': 0,
        'source_out_sample': count,
        'playback_speed': 1,
        'fade_in_samples': min(11025, count),
        'fade_out_samples': min(11025, count),
        'gain_db': 0,
        'gap_samples': 0,
        'overlap_samples': 0,
        'muted': False,
        'slot': {'type': 'background', 'spoken_text': name}
    }

    new_track = {
        'id': track_id,
        'name': name,
        'role': role,
        'gain_db': gain_db,
        'muted': False,
        'solo': False,
        'pan': 0
    }

    layers.append(new_layer)
    tracks.append(new_track)
    return layers, tracks

def remove_layer(base, ident):
    _validate_base(base)
    layers = copy.deepcopy(base['layers'])
    tracks = copy.deepcopy(base['tracks'])

    target_idx = None
    for i, l in enumerate(layers):
        if l.get('id') == ident:
            if l.get('clip_role') not in ('background_music', 'background_ambience'):
                raise ValueError("Not a background layer")
            target_idx = i
            break

    if target_idx is None:
        raise ValueError("Layer not found")

    for l in layers:
        if l.get('after_layer_id') == ident:
            raise ValueError("Cannot remove: other layers depend on this layer")

    target = layers[target_idx]
    track_id = target.get('track_id')

    layers.pop(target_idx)

    track_refs = [l.get('track_id') for l in layers]
    if track_id not in track_refs:
        tracks = [t for t in tracks if t.get('id') != track_id]

    return layers, tracks
