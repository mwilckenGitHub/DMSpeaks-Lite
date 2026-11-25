"""Finite replacement coverage; local Qwen draft corrected against the real parser."""
from engine_limits import MAX_SEGMENTS
import json
from model import parse_segments

def coverage_cases(text, player, domains, excluded=("name",),parser=None):
    parser=parser or parse_segments
    if not callable(parser):raise ValueError('Use a callable source parser.')
    if not isinstance(text, str):
        raise ValueError("text must be str")
    if not isinstance(player, dict):
        raise ValueError("player must be dict")
    if not isinstance(domains, dict):
        raise ValueError("domains must be dict")
    if len(domains) > MAX_SEGMENTS:
        raise ValueError(f"domains exceed the current capacity of {MAX_SEGMENTS} keys")
    for k in domains:
        if not isinstance(k, str):
            raise ValueError("domain keys must be str")
        if k in excluded:
            raise ValueError(f"excluded key supplied in domains: {k}")
        vals = domains[k]
        if not isinstance(vals, list) or not vals:
            raise ValueError(f"domain {k} must be nonempty list")
        if len(vals) > 64:
            raise ValueError(f"domain {k} must have <=64 values")
        seen = set()
        for v in vals:
            if v is None:
                raise ValueError(f"domain {k} contains None")
            if type(v) not in (bool, int, float, str):
                raise ValueError(f"domain {k} values must be JSON primitives")
            if isinstance(v, float) and (v != v or v in (float('inf'), float('-inf'))):
                raise ValueError(f"domain {k} contains non-finite float")
            sig = json.dumps(v, sort_keys=True)
            if sig in seen:
                raise ValueError(f"domain {k} has duplicate values")
            seen.add(sig)
    for k, v in player.items():
        if not isinstance(k, str):
            raise ValueError("player keys must be str")
        if v is None:
            continue
        if type(v) not in (bool, int, float, str):
            raise ValueError(f"player key {k} must be JSON primitive")
        if isinstance(v, float) and (v != v or v in (float('inf'), float('-inf'))):
            raise ValueError(f"player key {k} contains non-finite float")
    parsed = parser(text, player)
    if not isinstance(parsed, dict):
        raise ValueError("parse_segments must return dict")
    missing = parsed.get("missing_values", [])
    if missing:
        raise ValueError(f"missing values: {missing}")
    segments = parsed.get("segments", [])
    if not isinstance(segments, list):
        raise ValueError("segments must be list")
    if len(segments)>MAX_SEGMENTS:raise ValueError(f"Coverage exceeds the current runtime capacity of {MAX_SEGMENTS} segments")
    fixed_orders = [s["order"] for s in segments if s.get("type") == "fixed" and any(c.isalnum() for c in s.get("spoken_text", ""))]
    used_domain_keys = [key for key in domains if any(s["type"]==key and s["placeholder"] for s in segments)]
    if "fixed" in used_domain_keys:raise ValueError("fixed is a reserved segment type; rename that variable")
    baseline_target = [s["order"] for s in segments if s["order"] in fixed_orders or s["type"] in used_domain_keys]
    cases = []
    if baseline_target:
        cases.append({"player": dict(player), "target_orders": baseline_target, "variables": used_domain_keys})
    for key in used_domain_keys:
        seen = {tuple(s["value"] for s in segments if s["type"]==key)}
        for value in domains[key]:
            candidate = {**player,key:value}
            parsed_case = parser(text,candidate)
            if parsed_case["missing_values"]:raise ValueError(f"Domain {key} leaves unresolved placeholders: {parsed_case['missing_values']}")
            slots = [s for s in parsed_case["segments"] if s["type"]==key]
            signature = tuple(s["value"] for s in slots)
            if signature in seen:continue
            seen.add(signature)
            cases.append({"player":candidate,"target_orders":[s["order"] for s in slots],"variables":[key]})
            if len(cases)>1024:raise ValueError("max 1024 cases exceeded")
    return cases
