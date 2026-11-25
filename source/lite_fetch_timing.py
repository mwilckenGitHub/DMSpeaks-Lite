"""Keep fetch timings scoped to the same installation, character, provider and area."""

import json
from math import isfinite


def history_key(root, character_id, provider_id, coverage):
    if not all((root, character_id, provider_id, coverage)):
        return None
    return "lite_fetch_timing_" + json.dumps([str(root), character_id, provider_id, coverage], separators=(",", ":"))


def _saved_key(saved):
    if not isinstance(saved, dict):
        return None
    return history_key(*(saved.get(field) for field in ("root", "character_id", "provider_id", "coverage")))


def _seconds(value):
    return type(value) in (int, float) and isfinite(value) and value >= 0


def npc_timings(dialogues):
    """Sum measured dialogue work per speaker, including repeat visits in the same pass."""
    by_speaker = {}
    for row in dialogues:
        name = row.get("npc")
        if not name or not _seconds(row.get("elapsed_seconds")):
            continue
        key = row.get("npc_id") or name
        if key not in by_speaker:
            by_speaker[key] = {"npc": name, "elapsed_seconds": 0}
        by_speaker[key]["elapsed_seconds"] += row["elapsed_seconds"]
    return [{**row, "elapsed_seconds": round(row["elapsed_seconds"], 3)} for row in by_speaker.values()]


def _header(saved):
    status = saved.get("status", {})
    if not isinstance(status, dict) or not isinstance(status.get("state"), str):
        return None
    started_ns = saved.get("accepted_at_ns")
    finished_ns = saved.get("finished_at_ns")
    ident = saved.get("request_id") or status.get("job_id")
    if not ident and finished_ns:
        ident = str(finished_ns)
    duration = status.get("elapsed_seconds")
    if not _seconds(duration):
        if type(started_ns) is not int or type(finished_ns) is not int or not 0 < started_ns <= finished_ns:
            return None
        duration = (finished_ns - started_ns) / 1_000_000_000
    if not isinstance(ident, str) or not ident:
        return None
    entry = {"request_id": ident, "elapsed_seconds": round(duration, 3),
             "state": status["state"], "fetch_scope": status.get("fetch_scope", "area"), "dialogue_count": 0}
    if type(started_ns) is int and started_ns > 0:
        entry["started_at"] = started_ns / 1_000_000_000
    if type(finished_ns) is int and finished_ns > 0:
        entry["finished_at"] = finished_ns / 1_000_000_000
    for name in ("ready", "total"):
        if type(status.get(name)) is int:
            entry[name] = status[name]
    return entry


def _detail_key(key, request_id):
    return key + "/" + json.dumps(request_id)


def _rows(store, key):
    try:
        value = store.setting(key, [])
    except json.JSONDecodeError as error:
        # Keep the original text beside any new measurements on the next save.
        return [error.doc]
    return value if isinstance(value, list) else [value]


def _valid_pass(row):
    return (isinstance(row, dict) and isinstance(row.get("request_id"), str) and row["request_id"]
            and isinstance(row.get("state"), str) and _seconds(row.get("elapsed_seconds")))


def _pass_time(row):
    if isinstance(row, dict):
        value = row.get("finished_at", row.get("started_at"))
        if _seconds(value):
            return value
    return 0


def save_pass(store, saved, statuses=()):
    """A restored request has the same ID. Replaying completion must count it just once."""
    key = _saved_key(saved)
    if not key or not (entry := _header(saved)):
        return
    passes = _rows(store, key)
    for index, old in enumerate(passes):
        if _valid_pass(old) and old["request_id"] == entry["request_id"]:
            if old["state"] == "paused" and entry["state"] == "stopped":
                passes[index] = {**old, "state": "stopped"}
                store.set_setting(key, passes)
            return
    fields = ("source_id", "source_key", "npc_id", "npc", "stage", "state", "elapsed_seconds", "already_ready")
    dialogues = [{field: row[field] for field in fields if field in row}
                 for row in statuses if _seconds(row.get("elapsed_seconds"))]
    if dialogues:
        # Write details first so every visible header points to a complete report.
        store.set_setting(_detail_key(key, entry["request_id"]), dialogues)
        entry["dialogue_count"] = len(dialogues)
    store.set_setting(key, sorted([*passes, entry], key=_pass_time))


def timing_report(store, key, last_fetch):
    rows = _rows(store, key) if key else []
    passes = [row for row in rows if _valid_pass(row)]
    incomplete = len(passes) != len(rows)
    if key and _saved_key(last_fetch) == key:
        legacy = _header(last_fetch)
        if legacy and all(row["request_id"] != legacy["request_id"] for row in passes):
            passes = sorted([*passes, legacy], key=_pass_time)
    report = {"passes": passes, "total_seconds": round(sum(row["elapsed_seconds"] for row in passes), 3)}
    if incomplete:
        report["incomplete_history"] = True
    return report


def dialogue_timings(store, key, request_id):
    if not key or not isinstance(request_id, str) or not request_id:
        raise ValueError("Choose a fetch from the timing report.")
    return [row for row in _rows(store, _detail_key(key, request_id))
            if isinstance(row, dict) and _seconds(row.get("elapsed_seconds"))]
