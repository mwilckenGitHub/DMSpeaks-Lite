"""Local Qwen schema draft, reviewed for strict validation and finite numeric ranges."""
import math

EFFECTS = {
    "normalize_peak": {"defaults": {"target_db": -1}, "ranges": {"target_db": (-60, 0)}},
    "invert_polarity": {"defaults": {}, "ranges": {}},
    "fade_in": {"defaults": {"duration_ms": 10}, "ranges": {"duration_ms": (0, 5000)}},
    "fade_out": {"defaults": {"duration_ms": 10}, "ranges": {"duration_ms": (0, 5000)}},
    "highpass": {
        "defaults": {"cutoff_hz": 80},
        "ranges": {"cutoff_hz": (20, 2000)}
    },
    "lowpass": {
        "defaults": {"cutoff_hz": 16000},
        "ranges": {"cutoff_hz": (1000, 20000)}
    },
    "equalizer": {
        "defaults": {"frequency_hz": 1000, "q": 1, "gain_db": 0},
        "ranges": {
            "frequency_hz": (20, 20000),
            "q": (0.1, 10),
            "gain_db": (-18, 18)
        }
    },
    "compressor": {
        "defaults": {
            "threshold_db": -18,
            "ratio": 3,
            "attack_ms": 10,
            "release_ms": 100,
            "makeup_db": 0
        },
        "ranges": {
            "threshold_db": (-60, 0),
            "ratio": (1, 20),
            "attack_ms": (0.1, 200),
            "release_ms": (10, 2000),
            "makeup_db": (0, 12)
        }
    },
    "delay": {
        "defaults": {"delay_ms": 120, "wet": 0.15},
        "ranges": {
            "delay_ms": (10, 1000),
            "wet": (0, 0.6)
        }
    },
    "chorus": {
        "defaults": {"delay_ms": 50, "depth_ms": 2, "rate_hz": 0.25, "wet": 0.15},
        "ranges": {"delay_ms": (20, 80), "depth_ms": (0.1, 10),
                   "rate_hz": (0.1, 5), "wet": (0, 0.6)}
    },
    "reverb": {
        "defaults": {"tail_ms": 400, "wet": 0.12},
        "ranges": {
            "tail_ms": (50, 2000),
            "wet": (0, 0.6)
        }
    }
}

METADATA = {
    "version": "dmspeaks-fx-0.1",
    "effect_count": len(EFFECTS),
    "max_items": len(EFFECTS)
}

def _validate_number(key, value, low, high):
    if isinstance(value, bool):
        raise ValueError(f"Parameter '{key}' must be a number, not bool")
    if not isinstance(value, (int, float)):
        raise ValueError(f"Parameter '{key}' must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Parameter '{key}' must be finite")
    if value < low or value > high:
        raise ValueError(f"Parameter '{key}' out of range [{low}, {high}]")
    return float(value)

def validate_effects(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Input must be a list")
    if len(value) > len(EFFECTS):
        raise ValueError(f"Maximum {len(EFFECTS)} built-in effects allowed")

    seen_types = set()
    result = []

    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"Effect at index {i} must be a dict")

        allowed_keys = {"type", "enabled", "params"}
        unknown_keys = set(item.keys()) - allowed_keys
        if unknown_keys:
            raise ValueError(f"Unknown keys in effect at index {i}: {sorted(unknown_keys)}")

        if "type" not in item:
            raise ValueError(f"Effect at index {i} missing 'type'")

        etype = item["type"]
        if not isinstance(etype, str) or etype not in EFFECTS:
            raise ValueError(f"Unknown effect type: {etype}")

        if etype in seen_types:
            raise ValueError(f"Duplicate effect type: {etype}")
        seen_types.add(etype)

        enabled = True
        if "enabled" in item:
            enabled = item["enabled"]
            if not isinstance(enabled, bool):
                raise ValueError(f"'enabled' must be bool for effect at index {i}")

        params_in = item.get("params", {})
        if not isinstance(params_in, dict):
            raise ValueError(f"'params' must be a dict for effect at index {i}")

        spec = EFFECTS[etype]
        defaults = spec["defaults"]
        ranges = spec["ranges"]

        unknown_params = set(params_in.keys()) - set(defaults.keys())
        if unknown_params:
            raise ValueError(f"Unknown params for {etype}: {sorted(unknown_params)}")

        final_params = {}
        for key, default_val in defaults.items():
            if key in params_in:
                val = params_in[key]
            else:
                val = default_val

            low, high = ranges[key]
            final_params[key] = _validate_number(key, val, low, high)

        result.append({
            "type": etype,
            "enabled": enabled,
            "params": final_params
        })

    return result
