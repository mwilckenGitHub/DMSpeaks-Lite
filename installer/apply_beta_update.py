"""Update the installed Windows beta's program and minimap files in place."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import sys
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_addon
from open_lite import IDLE_STATES, installed_build_id, listening_pid, read_snapshot, switch_previous_listener


def apply_runtime(package, archive, manifest):
    source = package / "source"
    work = package / ".beta-update"
    install_addon.clean_or_recover(source, work)
    checked, missing = {source}, []
    entries = []
    for name, (old, new) in manifest["files"].items():
        found = install_addon.installed_digest(source, name, checked, missing)
        accepted = old if isinstance(old, list) else [old]
        if found != new and found not in accepted:
            raise ValueError(f"The installed Lite file does not match this beta: {name}")
        if found != new:
            if hashlib.sha256(archive.read(name)).hexdigest() != new:
                raise ValueError(f"The update file is damaged: {name}")
            entries.append((archive.getinfo(name), PurePosixPath(name)))
    return entries


def update(package, game, runtime_path, addon_path, *, retire):
    source = package / "source"
    work = package / ".beta-update"
    lock = package / ".beta-update.lock"
    if any(install_addon.is_link(path) for path in (source, work, lock)):
        raise ValueError("The installed Lite program path is linked.")
    with install_addon.placement_lock(lock), ZipFile(runtime_path) as archive:
        manifest = json.loads(archive.read("update.json"))
        # If an earlier attempt stopped after replacing files, restore it
        # before checking the exact code identity or trying another update.
        entries = apply_runtime(package, archive, manifest)
        before = installed_build_id(package)
        if before not in (manifest["before"], manifest["after"], *manifest.get("compatible_builds", [])):
            raise ValueError("This update is for the current DMSpeaks Windows local beta.")
        print("Checking the installed DMSpeaks files...", flush=True)
        if entries:
            retire(package, game, manifest["after"])
        install_addon.DELTA_SHA256 = manifest["addon_sha256"]
        # The matching delta contains every needed game file. A mismatch
        # fails without looking for or extracting a full audio archive.
        install_addon.install(game, addon_path.with_name("DMSpeaks-WoW.zip"), addon_path)
        if entries:
            replace_runtime(package, archive, entries, manifest["after"])
        print("DMSpeaks Lite is updated. Your settings and fetched audio are saved.", flush=True)


def replace_runtime(package, archive, entries, expected):
    source = package / "source"
    work = package / ".beta-update"
    try:
        state = install_addon.stage_update(source, work, archive, entries)
        marker = work / "transaction.json"
        install_addon.write_marker(marker, state)
        for relative in state["new_dirs"]:
            (source / relative).mkdir(exist_ok=True)
        for name in state["files"]:
            (work / "incoming" / name).replace(source / name)
        if installed_build_id(package) != expected:
            raise ValueError("Could not verify the updated Lite program.")
        install_addon.write_marker(marker, {**state, "committed": True})
    finally:
        install_addon.clean_or_recover(source, work)


def retire_listener(package, game, new_build):
    from wow_fetch_desktop import client_id, default_data_folder

    addons = game / "Interface/AddOns"
    snapshots = (addons / "DMSpeaksLiteStatus/Snapshot.lua", addons / "DMSpeaksLite/Status.lua")
    for path in snapshots:
        port, state = read_snapshot(path)
        if port and 0 < port <= 65535 and listening_pid(port) and state not in IDLE_STATES:
            raise ValueError("Pause name fetching in WoW and wait for Lite to report Paused, then retry.")
    switch_previous_listener(package, game, default_data_folder(), new_build, client_id)
    for path in snapshots:
        port, _ = read_snapshot(path)
        if port and 0 < port <= 65535 and listening_pid(port):
            from open_lite import probe

            if listener := probe(port, game, default_data_folder(), client_id):
                if listener["build"] != new_build:
                    raise ValueError("The previous DMSpeaks Lite window is still running. Close it and retry.")


if __name__ == "__main__":
    try:
        package = Path(sys.executable).resolve().parents[2]
        game, runtime_zip, addon_zip = (Path(value).resolve() for value in sys.argv[1:])
        update(package, game, runtime_zip, addon_zip, retire=retire_listener)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(str(error), flush=True)
        raise SystemExit(1)
