"""Build a Spoken companion add-on from the verified player-name manifests."""

from copy import deepcopy
from hashlib import file_digest, sha256
import json
from pathlib import Path
import re
from shutil import copyfileobj
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unicodedata import normalize
from zipfile import BadZipFile, ZIP_STORED, ZipFile

from lite_portable_io import (cache_bytes, digest, file_hash, filesystem_path, hold, manifest_render_fingerprint,
                              path_key, read, stock_binding, validate_routes)
from lite_portable_index import quote_path, render_index_entry
from project_recovery import WorkingCopyLease
from spoken_pack_lua import lua_string
from lite_source_identity import dialogue_source_tuple, manifest_key, quest_wording_key


ADDON = "DMSpeaksSpokenNames"
ARCHIVE = ADDON + ".zip"
INDEX = Path(ADDON) / "DialogueIndex.lua"
BOOTSTRAP_INDEX = b"local _, addon = ...\naddon.dialoguesByStockPath = {\n}\n"
PLAYER_NAME_LOADER = Path(__file__).resolve().parent / "lite_addon/NamePlayback.lua"
PLAYER_NAME_ICON = PLAYER_NAME_LOADER.with_name("Icons") / "player-names.tga"
# A typical Windows install adds about 55 characters before Interface. Keep
# the generated route within the 259 code units available on that install.
PACED_GAME_PATH_LIMIT = 195
# Exact loaders shipped in the private Windows test and coordinated previews.
UPGRADEABLE_PLAYBACK_LOADERS = {
    "a0fc9fe624b5fdb036e844d22cde16f364f7abbf831973051b191caa4e860749",
    "f80cbadc736d12f01202e86aee78b250de04d21a0af6b41913eaeff9efcc9a02",
    "7f0e650cf9f06f4eafe6e829f256e2df3ff0022d92e766b18eb077049b0a9415",
    "a8e495967d00172535d43bfec59811b2beef6f93bde6d0c5c5ef6c23acee1e68",
    "5ae296ee4c72a0cbdeb096bf43c155b8ab8344bffdfa6ccc6245fcee3c151082",
    "8ef80c00479da9c74ce5aca9eaf5695ab439789ebac40d562e6dc3d6cc2ed0d6",
    "762f1e9894f7645884f920196939df338f5ad3457e587db82dff1e426eaae068",
    "f8497aaa0c5612f6d5dac8f738c2726362dfd0b84d445966379103853440e710",
    "4485c85d99ac7e7dcc3fd805b59e130f497870ccb53cebd9f1f022b2e3a52d5c",
    "5571a4077d270f343bed916cfff3c91ae8db9258affbf45383f5959cd6a3874d",
    "3b45c0f3ef81ed85a16c110aff123c3e5150abe5d194396de474fd943b0ce92d",
    "3513feb518c613ef24e51295ec04d8fd8673e5b8f01ff59a75ae4d5bdf73eaf6",
    "c795de758ab404e008bcef2cffd8bdf09aa523fd31be36adf71d907ade4fb8bf",
}
ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
STOCK_SHA256_FIELD = re.compile(rb'stockSha256 = "([a-f0-9]{64})", ')
RENDER_SHA256_FIELD = re.compile(rb'renderSha256 = "([a-f0-9]{64})", ')


def playback_toc(*, legacy_names=False, store_playback=False):
    from lite_name_bridge import addon_metadata

    interface, version = addon_metadata()
    return (f"## Interface: {interface}\n"
            "## Title: DMSpeaks - Player Names\n"
            f"## Version: {version}\n"
            f"## IconTexture: Interface\\AddOns\\{ADDON}\\Icons\\{PLAYER_NAME_ICON.name}\n"
            "## Dependencies: Spoken, Spoken_Quests\n"
            "## OptionalDeps: DMSpeaksSpokenShared, DMSpeaksSpokenAlliance, DMSpeaksSpokenHorde"
            + (", DMSpeaksLiteStatus" if store_playback else "") + "\n"
            "## Group: DMSpeaksLite\n"
            "## X-Part-Of: DMSpeaksLite\n"
            "## X-Child-Of: DMSpeaksLite\n\n"
            "DialogueIndex.lua\n"
            + ("LegacyNames.lua\n" if legacy_names else "")
            + "Playback.lua\n")


def bootstrap_playback(addons):
    """Register the module before launch so first placement can use a UI reload."""
    from lite_name_automatic import addons_folder

    addons = addons_folder(str(addons.resolve()))
    target = addons / ADDON
    with hold(addons, f".{ADDON}.lock"):
        if target.is_symlink():
            raise ValueError("The DMSpeaks playback module leaves the selected AddOns folder.")
        if target.exists():
            if (target / f"{ADDON}.toc").is_file():
                return
            raise ValueError("The existing DMSpeaks playback folder is incomplete.")
        with TemporaryDirectory(dir=addons, prefix=".dmspeaks-playback-") as temporary:
            module = Path(temporary) / ADDON
            icon = module / "Icons" / PLAYER_NAME_ICON.name
            icon.parent.mkdir(parents=True)
            icon.write_bytes(PLAYER_NAME_ICON.read_bytes())
            (module / "DialogueIndex.lua").write_bytes(BOOTSTRAP_INDEX)
            (module / "Playback.lua").write_bytes(b"-- Playback is installed with the first verified audio.\n")
            (module / f"{ADDON}.toc").write_bytes(playback_toc().encode())
            module.rename(target)


def refresh_playback_loader(addons):
    """Upgrade a matching prior loader without rebuilding an installed library."""
    addons = addons.resolve()
    module = addons / ADDON
    loader = module / "Playback.lua"
    if not (module / f"{ADDON}.toc").is_file() or not loader.is_file():
        return
    with hold(addons, f".{ADDON}.lock"):
        if loader.resolve() == loader and file_hash(loader) in UPGRADEABLE_PLAYBACK_LOADERS:
            cache_bytes(loader, PLAYER_NAME_LOADER.read_bytes())


def entry_prefix(path):
    return f"\n    [{quote_path('Interface/AddOns/' + path)}] = {{ ".encode()


def index_binding(index, path, load_manifest):
    _, entry_found, entry = index.partition(entry_prefix(path))
    if not entry_found:
        return None
    stock_field = STOCK_SHA256_FIELD.match(entry)
    render_field = RENDER_SHA256_FIELD.match(entry, stock_field.end()) if stock_field else None
    if render_field:
        return (stock_field[1].decode("ascii"), render_field[1].decode("ascii"))
    # Older indexes stored their signed binding only in the installed manifest.
    relative = Path(*Path(path).parts[1:]).with_suffix("")
    manifest = load_manifest(Path(ADDON) / ADDON / relative / "dialogue.json")
    if isinstance(manifest, dict):
        binding = stock_binding(manifest)
        checksum = binding.get("sha256") if isinstance(binding, dict) else None
        if (isinstance(checksum, str) and re.fullmatch(r"[a-f0-9]{64}", checksum)
                and binding.get("path") == path):
            if not stock_field or checksum == stock_field[1].decode("ascii"):
                render = manifest_render_fingerprint(manifest) if "source_pcm_sha256" in manifest else None
                return checksum, render
    if stock_field:
        return stock_field[1].decode("ascii"), None


def disable_index_routes(index, stock_paths):
    """Keep unrelated index entries and Oggs while suppressing invalid bindings."""
    affected = set()
    header = b"local _, addon = ...\naddon.dialoguesByStockPath = {\n"
    if not index.startswith(header):
        raise ValueError("The installed DMSpeaks playback index has an unsupported format.")
    for path in sorted(stock_paths):
        key = quote_path("Interface/AddOns/" + path)
        if entry_prefix(path) not in index:
            continue
        affected.add(path)
        entry = f"addon.dialoguesByStockPath[{key}]"
        rule = ("\n-- A checked Spoken binding changed.\n"
                f"do\n    local stale = {entry}\n"
                f"    {entry} = nil\n"
                "    local aliases = addon.legacyAliases\n"
                "    if stale and aliases and aliases[stale.key] == stale then\n"
                "        aliases[stale.key] = nil\n"
                "    end\nend\n").encode()
        if rule not in index:
            index += rule
    return index, affected


def disable_installed_routes(addons, rejected_bindings, *, expected_render=None):
    root = addons.resolve()
    path = root / INDEX

    def load_manifest(relative):
        manifest_path = root / relative
        if manifest_path.resolve() == manifest_path and manifest_path.is_file():
            try:
                return read(manifest_path)
            except ValueError:
                pass
        return {}

    with hold(root, f".{ADDON}.lock"):
        if not path.is_file():
            return {}
        if path.resolve() != path:
            raise ValueError("The DMSpeaks playback index leaves the selected AddOns folder.")
        original = path.read_bytes()
        candidates = {}
        for stock_path, rejected in rejected_bindings.items():
            if entry_prefix(stock_path) not in original:
                continue
            active = index_binding(original, stock_path, load_manifest)
            if expected_render is not None:
                if active != (rejected, expected_render):
                    candidates[stock_path] = active
                continue
            installed_stock = (root / stock_path).resolve()
            if (active and active[0] != rejected and installed_stock.is_relative_to(root)
                    and installed_stock.is_file() and file_hash(installed_stock) == active[0]):
                # A check using another catalog does not supersede a valid installed route.
                continue
            candidates[stock_path] = active
        updated, affected = disable_index_routes(original, candidates)
        if updated != original:
            cache_bytes(path, updated)
        return {stock_path: candidates[stock_path] for stock_path in affected}


def disable_exported_routes(archive, disabled_bindings):
    with hold(archive.parent, ".package.lock"):
        if not archive.is_file():
            return
        with TemporaryDirectory(dir=archive.parent, prefix=".name-export-") as temporary:
            updated_zip = Path(temporary) / ARCHIVE
            try:
                with ZipFile(archive) as current:
                    index = current.read(INDEX.as_posix())

                    def load_manifest(path):
                        try:
                            return json.loads(current.read(path.as_posix()))
                        except (KeyError, ValueError):
                            return {}

                    paths = set()
                    for path, disabled in disabled_bindings.items():
                        exported = index_binding(index, path, load_manifest)
                        if (disabled is None or exported is None or disabled == exported
                                or (disabled[0] == exported[0] and None in (disabled[1], exported[1]))):
                            paths.add(path)
                    updated, _ = disable_index_routes(index, paths)
                    if updated == index:
                        return
                    with ZipFile(updated_zip, "w", compression=ZIP_STORED) as output:
                        for member in current.infolist():
                            if member.filename == INDEX.as_posix():
                                output.writestr(member, updated)
                            else:
                                with current.open(member) as source, output.open(member, "w") as target:
                                    copyfileobj(source, target, 1024 * 1024)
            except (BadZipFile, KeyError) as error:
                raise ValueError("The saved DMSpeaks export could not be updated.") from error
            updated_zip.replace(archive)


def package_names(packages, archive, *, include=None, trusted_deliveries=None, additional_roots=(),
                  legacy_playback_paths=False, decoded_clocks=None, legacy_addons=None, preserved_paced_names=None,
                  store_playback=False, store_predecessors=None):
    """Atomically publish prepared clips and the explicitly admitted paced recipes."""
    if store_predecessors and not store_playback:
        raise ValueError("Retained store recipes need a store playback package.")
    roots=[filesystem_path(path) for path in (packages,*additional_roots)]
    if len({path_key(root) for root in roots})!=len(roots):
        raise ValueError('A cumulative package root was supplied twice.')
    owners={path:root for root in roots for path in sorted(root.glob("*/generated/sounds/*/*/dialogue.json"))}
    sealed = {}
    seals_by_path = {}
    for name, seal in (trusted_deliveries or {}).items():
        folder = filesystem_path(name)
        if len(folder.parents) < 5 or folder.parts[-4:-2] != ('generated', 'sounds'):
            raise ValueError('Invalid admitted paced directory layout.')
        root = folder.parents[4]
        path = folder / 'dialogue.json'
        relative = path.relative_to(root)
        key = path_key(folder, resolve=False)
        if key != path_key(folder):
            raise ValueError("The paced receipt no longer points to its recorded cache location.")
        if relative in sealed or key in seals_by_path:
            raise ValueError('Two paced recipes use the same package path.')
        sealed[relative] = (path, root)
        seals_by_path[key] = seal
    # Use the same relative order for a consolidated collection and a
    # collection whose Name extensions live in separate input directories.
    for path, _ in sealed.values():
        owners.pop(path, None)
    owners.update(sealed[relative] for relative in sorted(sealed))
    manifests=list(owners)
    if not manifests:
        report = {"packaged": 0, "package_holds": []}
        if include is not None:
            report["packaged_stock_paths"] = []
        return report
    archive = filesystem_path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    lease = WorkingCopyLease(archive.parent, ".package.lock")
    try:
        return write_archive(packages, manifests, archive, include=include, trusted_deliveries=seals_by_path,
                             manifest_roots=owners, legacy_playback_paths=legacy_playback_paths,
                             decoded_clocks=decoded_clocks, legacy_addons=legacy_addons,
                             preserved_paced_names=preserved_paced_names, store_playback=store_playback,
                             store_predecessors=store_predecessors)
    finally:
        lease.close()


def install_names(archive, addons, *, expected_index=None, check_index=False, archive_sha256=None):
    """Install in a selected WoW AddOns folder and activate the index last."""
    addons = filesystem_path(filesystem_path(addons).resolve())
    if not (addons / "Spoken" / "Spoken.toc").is_file() or not (
            addons / "Spoken_Quests" / "Spoken_Quests.toc").is_file():
        raise ValueError("Choose the AddOns folder containing Spoken and Spoken Quests.")
    target = addons / ADDON
    lease = WorkingCopyLease(addons, f".{ADDON}.lock")
    try:
        if check_index:
            active=addons/INDEX
            actual=file_hash(active) if active.exists() else None
            if actual!=expected_index:
                raise ValueError('The installed companion changed before placement; reconcile its routes before retrying.')
        with filesystem_path(archive).open("rb") as archive_stream, ZipFile(archive_stream) as package:
            if archive_sha256 is not None:
                archive_stream.seek(0)
                if file_digest(archive_stream, "sha256").hexdigest() != archive_sha256:
                    raise ValueError("The installed playback archive changed.")
            entries = {}
            for member in package.infolist():
                path = Path(member.filename)
                if member.is_dir() or path.is_absolute() or ".." in path.parts or "\\" in member.filename or (
                        len(path.parts) < 2 or path.parts[0] != ADDON or path in entries):
                    raise ValueError("The player-name package has an invalid path.")
                destination = filesystem_path((addons / path).resolve())
                if not destination.is_relative_to(target):
                    raise ValueError("The player-name package points outside the add-on.")
                entries[path] = member
            loader = INDEX
            required = {loader, Path(ADDON) / "Playback.lua", Path(ADDON) / f"{ADDON}.toc"}
            if not required <= entries.keys():
                raise ValueError("The player-name package is missing its playback files.")
            # Read every member to validate its ZIP checksum before writes.
            # A previously installed Ogg keeps its meaning on retry.
            changes = {}
            for path, member in entries.items():
                with package.open(member) as stream:
                    expected = file_digest(stream, "sha256").hexdigest()
                destination = addons / path
                if destination.is_file():
                    if file_hash(destination) == expected:
                        continue
                    if path.suffix == ".ogg":
                        raise ValueError(f"A different name Ogg already occupies {path.name}.")
                changes[path] = expected
            for path in sorted(changes, key=lambda path: (
                    path == loader, path.suffix != ".ogg", path.suffix == ".toc", path.as_posix())):
                destination = addons / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                with NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                    stage = Path(stream.name)
                    try:
                        with package.open(entries[path]) as source:
                            copyfileobj(source, stream, 1024 * 1024)
                        stream.close()
                        if file_hash(stage) != changes[path]:
                            raise OSError(f"Player-name install staging failed for {path.name}.")
                        stage.replace(destination)
                    finally:
                        stage.unlink(missing_ok=True)
                if file_hash(destination) != changes[path]:
                    raise OSError(f"Player-name install readback failed for {path.name}.")
            return bool(changes)
    finally:
        lease.close()


def prepare_published_names(manifest, referenced):
    routes = (manifest["sequence"], *manifest.get("character_sequences", {}).values())
    repairs = {}
    for route in routes:
        for part in route:
            if part["kind"] != "name":
                continue
            for value, clip in part["choices"].items():
                published = clip.get("published_file")
                if published is None:
                    continue
                stem = f"{manifest['spoken_key']}-name-{value}-{part['occurrence']}"
                version = digest(clip["request_sha256"].encode())[:20]
                if (not clip.get("name_article") or published != stem + ".ogg"
                        or Path(published).name != published or clip["file"] != f"{stem}-{version}.ogg"):
                    raise ValueError("The repaired Name has an invalid delivery filename.")
                repairs[clip["file"]] = (published, clip["sha256"])
    if not repairs:
        return manifest, {}, None

    aliases = {file: published for file, (published, _) in repairs.items()}
    if len({aliases.get(file, file) for file in referenced}) != len(referenced):
        raise ValueError("Two repaired clips share a delivery filename.")
    published_manifest = deepcopy(manifest)
    for route in (published_manifest["sequence"], *published_manifest.get("character_sequences", {}).values()):
        for part in route:
            for clip in part.get("choices", {}).values():
                if "published_file" in clip:
                    clip["file"] = clip.pop("published_file")
    revision = digest(json.dumps([manifest_render_fingerprint(manifest), repairs], sort_keys=True).encode())[:20]
    return published_manifest, aliases, revision


def paced_game_path_length(path):
    return len((Path("Interface/AddOns") / path).as_posix().encode("utf-16-le")) // 2


def compact_paced_paths(manifest, folder, filenames, referenced):
    """Use a short delivery revision for oversized paths after verifying the source."""
    if all(paced_game_path_length(folder / filenames.get(name, name)) <= PACED_GAME_PATH_LIMIT
           for name in referenced):
        return manifest, folder, filenames

    # Keep the prior revision intact for WoW sessions that already loaded it.
    key = digest(folder.as_posix().encode())[:24]
    compact = Path(ADDON) / f"p-{key}" / Path(*folder.parts[2:])
    short_key = "d-" + digest(manifest["spoken_key"].encode())[:12]
    rewritten = deepcopy(manifest)
    aliases = filenames.copy()
    current_names = {aliases.get(name, name): name for name in referenced}
    routes = (rewritten["sequence"], *rewritten.get("character_sequences", {}).values())
    for route in routes:
        for part in route:
            kind = part["kind"]
            clips = [part["clip"]] if kind == "fixed" else part["choices"].values()
            for clip in clips:
                published = clip["file"]
                source = current_names[published]
                if source in aliases and aliases[source] != published:
                    clip["file"] = aliases[source]
                    continue
                if paced_game_path_length(compact / published) <= PACED_GAME_PATH_LIMIT:
                    continue
                token = digest(published.encode())[:16]
                _, name_marker, name_suffix = published.rpartition("-name-")
                if kind == "name" and name_marker:
                    # Older caption lookup still reads the name from this suffix.
                    alias = f"d-{token}{name_marker}{name_suffix}"
                else:
                    occurrence = part.get("occurrence", 1)
                    alias = f"{short_key}-{kind}-{token}-{occurrence}.ogg"
                aliases[source] = alias
                clip["file"] = alias

    published = [aliases.get(name, name) for name in referenced]
    if len({name.casefold() for name in published}) != len(published):
        raise ValueError("Two paced clips share a compact playback filename.")
    if any(paced_game_path_length(compact / name) > PACED_GAME_PATH_LIMIT for name in published):
        raise ValueError("A paced dialogue exceeds the playback path budget.")
    return rewritten, compact, aliases


def write_archive(packages, manifests, archive, *, include=None, trusted_deliveries=None, manifest_roots=None,
                  legacy_playback_paths=False, decoded_clocks=None, legacy_addons=None, preserved_paced_names=None,
                  store_playback=False, store_predecessors=None):
    entries, generated, held = {}, {}, []
    native_quests,native_gossip,native_sources,native_included={},{},set(),set()
    paced_stock_quests, paced_stock_followups, native_followups = {}, {}, {}
    stock_keys = {}
    spellings = {}
    admitted_routes={}
    store_seals = {}
    retained_seals, retained_index = {}, {}
    pending_predecessors = dict(store_predecessors or {})
    legacy = None
    if legacy_addons is not None or preserved_paced_names:
        from lite_legacy_names import LegacyNames
        legacy = LegacyNames(legacy_addons, ADDON)
    preserved = {}
    if preserved_paced_names:
        from lite_paced_delivery import validate_projection

        for path, seal in preserved_paced_names.items():
            folder = filesystem_path(path)
            before = read(folder / "dialogue.json")
            validate_projection(folder, before, seal, decoded_clocks=decoded_clocks)
            owner = (dialogue_source_tuple(before), quest_wording_key(before))
            preserved.setdefault(owner, []).append((folder, before))
    with TemporaryDirectory(dir=archive.parent, prefix=".name-package-") as temporary:
        scratch = Path(temporary)
        candidate = scratch / ARCHIVE
        # Some installer assets carry the ZIP epoch in UTC, which is 1979 in some time zones.
        with ZipFile(candidate, "w", compression=ZIP_STORED, strict_timestamps=False) as output:
            for path in manifests:
                manifest = read(path)
                if include is not None and not include(manifest):
                    continue
                names = set(manifest.get("character_sequences", {}))
                measured = [{name for name, clip in part["choices"].items() if "measured_name" in clip}
                            for part in manifest["sequence"] if part["kind"] == "name"]
                if measured:
                    names.update(set.intersection(*measured))
                if not names and not (manifest.get('native_source') or manifest.get('paced_source')):
                    continue
                # Native and signed indexes can resolve the same dialogue. A
                # paced fallback must never shadow an existing named recipe.
                route_key = dialogue_source_tuple(manifest)
                wording = quest_wording_key(manifest)
                is_paced = bool(manifest.get('paced_source'))
                prior = admitted_routes.setdefault(route_key, [])
                if any((prior_paced or is_paced) and (
                        wording is None or old_wording is None or wording == old_wording)
                       for prior_paced, old_wording in prior):
                    raise ValueError('A paced and existing recipe overlap; reconcile their character choices before publishing.')
                prior.append((is_paced, wording))
                for sequence in (manifest["sequence"], *manifest.get("character_sequences", {}).values()):
                    for part in sequence:
                        if part["kind"] != "name":
                            continue
                        for spelling in part["expected_values"]:
                            name = spelling.casefold()
                            if name in names:
                                spellings.setdefault(name, set()).add(spelling)
                stock = stock_binding(manifest)
                relative = path.parent.relative_to((manifest_roots or {}).get(path,packages))
                key = stock["path"] if stock else manifest["spoken_key"]
                registry = entries if stock else generated
                native=manifest.get('native_source')
                if manifest.get('paced_source'):native=True
                if native:
                    route=manifest['native_route']
                    identity=(route['kind'],route['key'],route['npc_id'],route['original_text'])
                    if identity in native_sources:
                        held.append(dict(dialogue=manifest['spoken_key'],reason='More than one native recipe has the same event, giver and text.'))
                        continue
                    native_sources.add(identity)
                if not native and key in registry:
                    held.append({"dialogue": manifest["spoken_key"],
                                 "reason": "More than one staged recording uses this Spoken lookup."})
                    continue
                if manifest.get('paced_source'):
                    from lite_paced_delivery import validate_projection
                    seal=(trusted_deliveries or {}).get(path_key(path.parent))
                    referenced = validate_projection(path.parent, manifest, seal, decoded_clocks=decoded_clocks)
                    if legacy is not None and legacy.existing:
                        metadata = (path.parent / "native-selection-metadata.json").read_bytes()
                        if digest(metadata) != seal["native_selection_metadata_sha256"]:
                            raise ValueError("The admitted source metadata changed during legacy Name migration.")
                        legacy.add(output, manifest, json.loads(metadata))
                    for former_folder, former in preserved.pop((route_key, wording), ()):
                        kept_names = legacy.add_preserved(output, manifest, former_folder, former)
                        for part in former["sequence"]:
                            if part["kind"] == "name":
                                for spelling in part["expected_values"]:
                                    if spelling.casefold() in kept_names:
                                        spellings.setdefault(spelling.casefold(), set()).add(spelling)
                else:
                    referenced = validate_routes(path.parent, manifest, scratch, complete_names=True)
                stock_sha256 = stock.get("sha256") if stock else None
                if stock_sha256:
                    for sequence in (manifest["sequence"], *manifest.get("character_sequences", {}).values()):
                        for part in sequence:
                            clips = (part["clip"],) if part["kind"] == "fixed" else part["choices"].values()
                            if any(clip["sha256"] == stock_sha256 for clip in clips):
                                raise ValueError("The playback pack repeats the full stock Spoken recording.")
                manifest, filenames, revision = prepare_published_names(manifest, referenced)
                folder = Path(ADDON) / relative
                if revision or manifest.get("companion_seed") or manifest.get("reference_contract") or manifest.get('delivery_clock_format'):
                    # A loaded index can still refer to the previous seed. Keep both
                    # sets of clips readable until WoW loads the newly installed index,
                    # including changes to the admitted voice-reference policy.
                    revision = revision or manifest_render_fingerprint(manifest)[:20]
                    media_root, *dialogue_path = relative.parts
                    folder = Path(ADDON) / f"{media_root}-{revision}" / Path(*dialogue_path)
                if store_playback:
                    if not is_paced or filenames or names or any(
                            part["fallback"] != "adventurer" or set(part["choices"]) != {"adventurer"}
                            for part in manifest["sequence"] if part["kind"] == "name"):
                        raise ValueError("The shared playback pack needs an audited base with only fallback Names.")
                    from lite_store_playback import media_folder, stored_clip_path
                    folder = media_folder(manifest, seal)
                    media = folder.as_posix()
                    if media in store_seals:
                        raise ValueError("Two sealed stock routes share one playback identity.")
                    aliases = {
                        name: "clip-" + sha256(name.encode()).hexdigest()[:28] + ".ogg"
                        for name in referenced if name.endswith(".ogg")
                        and paced_game_path_length(folder / name) > PACED_GAME_PATH_LIMIT
                    }
                    if len(set(aliases.values())) != len(aliases) or set(aliases.values()) & referenced:
                        raise ValueError("Two shared clips have the same compact playback filename.")
                    if any(paced_game_path_length(folder / alias) > PACED_GAME_PATH_LIMIT
                           for alias in aliases.values()):
                        raise ValueError("A shared dialogue needs a shorter Windows playback path.")
                    store_seals[media] = {**seal, "store_clip_paths": aliases}
                    for name in sorted(referenced | {"dialogue.json"}):
                        destination = (folder / aliases.get(name, name)).as_posix()
                        if destination in output.NameToInfo:
                            raise ValueError("A shared clip overlaps a retained recipe.")
                        origin = stored_clip_path(path.parent, seal, name) if name.endswith(".ogg") else path.parent / name
                        output.write(origin, destination)
                    revision = seal["clock_contract_sha256"]
                    if predecessors := pending_predecessors.pop(revision, None):
                        from lite_store_playback import write_retained_recipes

                        retained_seals[revision], retained_index[revision] = write_retained_recipes(
                            output, manifest, revision, predecessors, decoded_clocks=decoded_clocks)
                elif is_paced and not legacy_playback_paths:
                    manifest, folder, filenames = compact_paced_paths(manifest, folder, filenames, referenced)
                if not store_playback:
                    for name in sorted(referenced | {"dialogue.json"}):
                        destination = (folder / filenames.get(name, name)).as_posix()
                        if name == "dialogue.json" and filenames:
                            output.writestr(destination, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
                        else:
                            output.write(path.parent / name, destination)
                game_manifest = manifest
                if store_playback and aliases:
                    game_manifest = deepcopy(manifest)
                    for part in game_manifest["sequence"]:
                        items = [part["clip"]] if part["kind"] == "fixed" else part["choices"].values()
                        for item in items:
                            item["file"] = aliases.get(item["file"], item["file"])
                if native:
                    entry=render_index_entry(game_manifest,folder,None,store_seal=seal if store_playback else None)
                    if route['kind']=='quest':
                        native_quests.setdefault(route['key'],[]).append(entry)
                        if manifest.get('paced_source') and stock:
                            paced_stock_quests.setdefault(stock['path'],[]).append(entry)
                    elif route['kind']=='gossip':
                        native_gossip.setdefault(route['npc_id'],[]).append(entry)
                        if manifest.get('paced_source'):
                            for alias in manifest.get('npc_aliases',[]):
                                native_gossip.setdefault(alias['npc_id'],[]).append(entry)
                    elif route['kind']=='followup' and stock:
                        paced_stock_followups.setdefault(stock['path'], []).append(entry)
                    elif route['kind']=='followup' and 'broadcast_id' in route:
                        native_followups.setdefault(route['broadcast_id'], []).append(entry)
                    else:raise ValueError('The native source needs an explicit event adapter.')
                    native_included.add(manifest_key(manifest))
                else:
                    registry[key] = render_index_entry(manifest, folder, stock["path"] if stock else None, names=names)
                if stock:
                    stock_keys.setdefault(manifest["spoken_key"], []).append(stock["path"])
            aliases = []
            for key, paths in sorted(stock_keys.items()):
                if len(paths) == 1 and key not in generated:
                    stock_path = quote_path("Interface/AddOns/" + paths[0])
                    aliases.append(f"    [{lua_string(key)}] = addon.dialoguesByStockPath[{stock_path}],")
            # WoW's Lua lowercases ASCII. Index the spellings seen in the
            # manifest and their usual case/Unicode forms without changing
            # the filenames or the name sent to the speech provider.
            name_aliases = {}
            ambiguous = set()
            for key, names in spellings.items():
                for name in names:
                    if name.isascii():
                        continue
                    for form in (name, name.lower(), name.upper(), name.title(), key):
                        for unicode_form in ("NFC", "NFD"):
                            alias = normalize(unicode_form, form).translate(ASCII_LOWER)
                            if name_aliases.setdefault(alias, key) != key:
                                ambiguous.add(alias)
            native_index='addon.questVariantsWithoutStock = {\n'
            for key,items in sorted(native_quests.items()):
                native_index+=f'    [{lua_string(key)}] = {{\n'+ '\n'.join(items)+'\n    },\n'
            native_index+='}\naddon.gossipByGiver = {\n'
            for key,items in sorted(native_gossip.items()):
                native_index+=f'    [{key}] = {{\n'+ '\n'.join(items)+'\n    },\n'
            native_index+='}\naddon.questVariantsByStockPath = {\n'
            for key,items in sorted(paced_stock_quests.items()):
                native_index+=f'    [{quote_path("Interface/AddOns/"+key)}] = {{\n'+'\n'.join(items)+'\n    },\n'
            native_index+='}\naddon.followupVariantsByStockPath = {\n'
            for key,items in sorted(paced_stock_followups.items()):
                native_index+=f'    [{quote_path("Interface/AddOns/"+key)}] = {{\n'+'\n'.join(items)+'\n    },\n'
            native_index+='}\naddon.followupVariantsByBroadcast = {\n'
            for key,items in sorted(native_followups.items()):
                native_index+=f'    [{key}] = {{\n'+'\n'.join(items)+'\n    },\n'
            native_index+='}\n'
            if pending_predecessors:
                raise ValueError("A retained Name recipe has no installed successor.")
            if retained_index:
                native_index += "addon.retainedStoreNames = {\n"
                for revision, items in sorted(retained_index.items()):
                    native_index += f"    [{lua_string(revision)}] = {{\n" + "\n".join(items) + "\n    },\n"
                native_index += "}\n"
            output.writestr(f"{ADDON}/DialogueIndex.lua",
                            "local _, addon = ...\naddon.dialoguesByStockPath = {\n"
                            + "\n".join(entries.values()) + "\n}\naddon.dialoguesWithoutStock = {\n"
                            + "\n".join(generated.values()) + "\n}\naddon.legacyAliases = {\n"
                            + "\n".join(aliases) + "\n}\naddon.nameAliases = {\n"
                            + "\n".join(f"    [{lua_string(alias)}] = {lua_string(key)},"
                                        for alias, key in sorted(name_aliases.items())
                                        if alias not in ambiguous) + "\n}\n"+native_index)
            if store_playback:
                from lite_store_playback import PLAYBACK
                playback_index = {"format": "dmspeaks-store-playback-1", "seals": store_seals}
                if retained_seals:
                    playback_index["retained"] = retained_seals
                output.writestr(PLAYBACK.as_posix(), json.dumps(playback_index, sort_keys=True))
            output.write(PLAYER_NAME_LOADER, f"{ADDON}/Playback.lua")
            output.write(PLAYER_NAME_ICON, f"{ADDON}/Icons/{PLAYER_NAME_ICON.name}")
            if preserved:
                raise ValueError("A preserved paced Name is missing its active dialogue.")
            legacy_names = legacy is not None and legacy.write_index(output)
            output.writestr(f"{ADDON}/{ADDON}.toc", playback_toc(legacy_names=legacy_names, store_playback=store_playback))
        if entries or generated or native_sources:
            candidate.replace(archive)
    report = {"packaged": len(entries) + len(generated)+len(native_sources), "package_holds": held}
    if legacy is not None and legacy.existing:
        report["legacy_name_recipes"] = sum(len(entries) for table in (legacy.quest, legacy.followup) for entries in table.values())
        report["legacy_name_holds"] = legacy.rejections
    if include is not None:
        report["packaged_stock_paths"] = sorted(entries)
        report['packaged_source_keys']=sorted(set(entries)|native_included)
    return report
