"""Project the verified release's Lua registrations for the desktop audio preview.

Run this maintainer tool in the Lua replay environment. Setup and the desktop
read the generated data without importing Lua or opening a network connection.
"""
import argparse
from collections import defaultdict
from contextlib import ExitStack
from hashlib import sha256
from itertools import product
import json
from pathlib import Path
import sys
from zipfile import ZipFile

from lupa.lua51 import LuaRuntime, lua_type

from join_repair_overlay import ADDON, MANIFEST, PROGRAM, TOC, load_join_overlay
from spoken_game_media import PACK_ROOTS

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "source"))
from lite_context_routes import ContextUnavailable, entry_for, read_table

REGIONS = ("Alliance", "Horde", "Shared", "Gossip")
PREVIEW_FIELDS = (
    "key", "rate", "sequence", "storeRevision", "npc", "allowedRaces", "allowedClasses",
    "allowedSex", "playerSex", "names", "worldStateText", "byName", "playbackRepairByClass", "playbackRepair",
)
CAPTION_FIELDS = {
    "first", "last", "caption", "captionPolicy", "captionStarts", "head", "tail", "articleFrom", "articleTo", "starts",
}
COLLECT = """
return function(root)
    local seen, entries, bound = {}, {}, {}
    local function visit(row)
        if type(row) ~= 'table' or seen[row] then return end
        seen[row] = true
        if row.sequence and row.storeRevision then entries[#entries+1] = row end
        for key, value in pairs(row) do
            if key ~= 'playbackPackOwners' then visit(value) end
        end
    end
    local function stock(row, path)
        if type(row) ~= 'table' then return end
        if row.storeRevision then
            local paths = bound[row.storeRevision] or {}
            paths[path] = true
            bound[row.storeRevision] = paths
        else
            for _, value in pairs(row) do stock(value, path) end
        end
    end
    visit(root)
    for _, name in ipairs({'questVariantsByStockPath','followupVariantsByStockPath','dialoguesByStockPath'}) do
        for path, row in pairs(root[name] or {}) do stock(row, path) end
    end
    return entries, bound
end
"""


def digest(value):
    return sha256(value).hexdigest()


def literal(value):
    if lua_type(value) == "table":
        value = dict(value.items())
    if isinstance(value, dict):
        fields = sorted(value.items(), key=lambda pair: (type(pair[0]).__name__, pair[0]))
        items = ("[" + literal(key) + "]=" + literal(child)
                 for key, child in fields if key not in CAPTION_FIELDS)
        return "{" + ",".join(items) + "}"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".15f").rstrip("0").rstrip(".")
    raise TypeError(f"Unsupported playback recipe value {type(value).__name__}.")


def project(entry):
    get_value = entry.get if isinstance(entry, dict) else entry.__getitem__
    return literal({key: value for key in PREVIEW_FIELDS if (value := get_value(key)) is not None})


def project_registrations(read, modules, enabled, revisions):
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.globals().GetAddOnMetadata = lambda name, key: (
        "2.7.3" if key == "Version" and name.removeprefix("SpokenQuestsAudio") in enabled else None
    )
    addon = lua.table()
    for filename in modules:
        lua.execute(read(f"{ADDON}/{filename}"), ADDON, addon)
    entries, bound = lua.execute(COLLECT)(addon)
    result = defaultdict(set)
    for entry in entries.values():
        revision = entry["storeRevision"]
        if revision in revisions:
            result[revision].add(project(entry))
    stock = {revision: set(paths.keys()) for revision, paths in bound.items() if revision in revisions}
    return result, stock


def make(archives, bundle, output):
    overlay, _ = load_join_overlay(bundle, archives, ROOT / "source/lite_addon/NamePlayback.lua")
    if PROGRAM not in overlay:
        raise ValueError("Playback in Context needs the reviewed join playback program.")
    with ExitStack() as stack:
        sources = {}
        for path in archives:
            archive = stack.enter_context(ZipFile(path))
            sources.update({name: archive for name in archive.namelist()})

        def read(name):
            return overlay[name] if name in overlay else sources[name].read(name)

        managed = read(f"{ADDON}/ManagedPlayback.json")
        index = read(f"{ADDON}/DialogueIndex.lua")
        old = json.loads(read(f"{ADDON}/RecordingContexts.json"))
        catalog = json.loads(managed)
        seals = {seal["clock_contract_sha256"]: (folder, seal)
                 for records in (catalog["seals"], *catalog["retained"].values())
                 for folder, seal in records.items()}
        baseline = {}
        for revision in seals:
            try:
                baseline[revision] = (project(entry_for(index, revision)),)
            except ContextUnavailable:
                continue
        for revision, raw in old["recipes"].items():
            if revision in seals and revision not in baseline:
                baseline[revision] = (project(read_table(raw.encode("utf-8"))),)

        modules = [line for line in read(TOC).decode("utf-8-sig").splitlines()
                   if line.endswith(".lua") and line not in ("Playback.lua", "JoinRepairs.lua")]
        changes = defaultdict(dict)
        stock_modes = []
        for mode, options in enumerate(product((False, True), repeat=len(REGIONS))):
            enabled = {region for region, value in zip(REGIONS, options) if value}
            entries, stock = project_registrations(read, modules, enabled, seals)
            stock_modes.append(stock)
            for revision in seals:
                active = tuple(sorted(entries.get(revision, ())))
                if active != baseline.get(revision, ()):
                    changes[revision][mode] = active

        # A standalone registration can remain after a stock-pack update removes
        # its old stock entry. A saved take is bound to the sealed native route.
        for revision in set().union(*(stock.keys() for stock in stock_modes)):
            paths = [stock.get(revision, set()) for stock in stock_modes]
            if all(path == paths[0] for path in paths[1:]):
                continue
            folder, seal = seals[revision]
            raw = read(folder + "/native-selection-metadata.json")
            if digest(raw) != seal["native_selection_metadata_sha256"]:
                raise ValueError("A native dialogue binding differs from the managed catalog.")
            native = json.loads(raw)
            original = (native.get("original_spoken") or {}).get("path")
            if original:
                path = ("Interface/AddOns/" + original).replace("/", chr(92))
                if any(path in known for known in paths):
                    for mode, known in enumerate(paths):
                        if path not in known:
                            changes[revision][mode] = ()

        recipes = {}
        for revision in sorted(changes.keys() | (old["recipes"].keys() & seals.keys())):
            modes = defaultdict(int)
            for mode in range(len(stock_modes)):
                entries = changes.get(revision, {}).get(mode, baseline.get(revision, ()))
                modes[entries] |= 1 << mode
            recipes[revision] = [{"modes": mask, "entries": entries} for entries, mask in modes.items()]
        registries = {name: digest(read(f"{ADDON}/{name}")) for name in modules
                      if name not in ("DialogueIndex.lua", "OriginalCuts.lua")}
        registries["JoinRepairs.lua"] = digest(read(f"{ADDON}/JoinRepairs.lua"))
        result = {
            "format": "dmspeaks-reviewed-name-contexts-2",
            "managed_sha256": digest(managed), "index_sha256": digest(index), "toc_sha256": digest(read(TOC)),
            "registries": registries, "join_repairs_sha256": digest(read(MANIFEST)),
            "regions": REGIONS, "recipes": recipes,
        }
    raw = (json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    output.write_bytes(raw)
    print(f"{digest(raw)}  {output.name} ({len(recipes)} supplemental revisions)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", type=Path, required=True, help="Directory of the verified release media ZIPs")
    parser.add_argument("--join-overlay", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    make([args.media / name for name in ("DMSpeaks-WoW.zip", *PACK_ROOTS)], args.join_overlay, args.output)
