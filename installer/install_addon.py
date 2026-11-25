"""Place the exact local beta archive in WoW's AddOns folder."""
from argparse import ArgumentParser, SUPPRESS
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, ExitStack
from gzip import GzipFile
from hashlib import file_digest, sha256
from itertools import islice
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
from stat import FILE_ATTRIBUTE_READONLY, FILE_ATTRIBUTE_REPARSE_POINT, S_ISDIR, S_ISREG
import sys
from time import monotonic, sleep
from types import SimpleNamespace
from zipfile import ZipFile
from zlib import crc32


ARCHIVE_SHA256 = "ad947819847e76ecf3c86f4637813e8d7c0101301bcb9d90fc1e10b2a3ad07a3"
DELTA_SHA256 = "eec5188d1682f83633be8742b66ccb5a4a167e3448ebc004c24cf5e4c57e98e6"
OWNERS_SHA256 = "eb20bb5df02d2b41b9c99004e2ec3dc4c044a4435b61bba2ec9d2da8a5eb4f75"
STOCK_SHA256 = "96fd58cfb0205c4a2100cec8e31962013b54486b5baffe53c933c55b8391d5f6"
ROOTS = ("DMSpeaksLite", "DMSpeaksNameData", "DMSpeaksSpokenAlliance",
         "DMSpeaksSpokenGossip", "DMSpeaksSpokenHorde", "DMSpeaksSpokenNames", "DMSpeaksSpokenShared",
         "DMSpeaksPersonalizedAlliance", "DMSpeaksPersonalizedGossip", "DMSpeaksPersonalizedHorde", "DMSpeaksPersonalizedShared")
LOCK = ".dmspeaks-beta-install.lock"
WORK = ".dmspeaks-beta-install"
CATALOG = PurePosixPath("DMSpeaksNameData/NameFetch/Prepared-dialogue-plans.zip")
RETIRED_MANIFESTS = {'DMSpeaksNameData/DMSpeaksNameData.toc': ['44575ff8f4859d8b64bee032a35a814a11443f486edec4c66786485c991d3357', 'f55578b2fb978afe1ff9f90c6a64f1671650104a3441aa6f844ba27f1a7da831', 'f88fcf3a1bc5546481a87009d9ed3118b2cc7431c8fff37d894fdfacadf76fc1'], 'DMSpeaksSpokenAlliance/DMSpeaksSpokenAlliance.toc': ['05116aac7a2378d804466b02537c5c8b524c758aab84cd8d3c6b9ac56669bd72', '48633c96dc8ddfb03637472ebf11467253300431d66090072bdd48b42c82fdad', 'acd4be55a2b1669066445eeaa804af5cee06ae3bd57d49d0674fd1ed72a25067'], 'DMSpeaksSpokenGossip/DMSpeaksSpokenGossip.toc': ['426e69059ff7f5c2f41d290daaeafc4fd2fba3d362a483e385ca581110091519', '46b5d35f0e2d4c696f4e8c51e003f4a1b006a4b9f9cb463740c3e43a359ece6e', 'e532719876ec2b8925a7c05986f404a74b8dd9f2e6d23350bcf21ccb2ae7def5'], 'DMSpeaksSpokenHorde/DMSpeaksSpokenHorde.toc': ['32aa3e1b362a40fba78015b790ef9968e58619b3ed20e3796507e908a4173b26', '59355f72701310c2c6706deaeebd363d5ef768520cfa460aec044f893ada6334', '8717484e47833fe513550daa598b574427a8e36e33f84dab67b7d6b36683fc50'], 'DMSpeaksSpokenShared/DMSpeaksSpokenShared.toc': ['512c4007adb6e8d6d538a45153e22665151cb587c498b8db5613993a9d224afa', '5235e4063b28ff2fbdd0a2dd22208238b6cc578302e94882cb83cfa321ca0f4f', 'c723af5d5562e69e9d37f1bfe98690c4c19132e8155b20fba42302fc10cd9fd8']}


class CatalogInUse(ValueError):
    pass


class NameSyncPending(ValueError):
    pass


class RepairCancelled(Exception):
    """The user canceled before any staged repair files were applied."""


class PlacementInUse(OSError):
    """A Windows reader held a file beyond the placement retry period."""


def wait_for_placement(operation, path):
    """Retry transient Windows sharing violations without repeating extraction."""
    deadline = monotonic() + 8
    delay = 0.1
    while True:
        try:
            return operation()
        except OSError as error:
            code = getattr(error, "winerror", None)
            # Windows can report a locked descendant as access denied when
            # renaming its parent directory.
            if code not in (32, 33) and not (code == 5 and path.is_dir()):
                raise
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise PlacementInUse(f"Windows still blocks access to {error.filename or path}.") from error
            if delay == 0.1:
                print(f"Waiting for Windows to release {path.name}...", flush=True)
            sleep(min(delay, remaining))
            delay = min(delay * 2, 1)


def move_placement(source, target, *, replace=False):
    move = source.replace if replace else source.rename
    return wait_for_placement(lambda: move(target), source)


@contextmanager
def placement_lock(path):
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def is_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def windows_api_path(path):
    """Use Windows file APIs without the legacy 260-character limit."""
    text = str(path)
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def check_game(wow):
    addons = wow / "Interface/AddOns"
    has_game = (wow / "Wow.exe").is_file()
    if sys.platform == "darwin":
        has_game = has_game or (wow / "World of Warcraft.app/Contents/Info.plist").is_file()
    if (not has_game or not addons.is_dir()
            or not all((addons / name / (name + ".toc")).is_file()
                       for name in ("Spoken", "Spoken_Quests"))):
        raise ValueError("Choose the WoW Forever folder with Spoken installed.")
    return addons


def write_marker(path, data):
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(data), encoding="utf-8")
    move_placement(pending, path, replace=True)


def restore(addons, work, state):
    for name in reversed(ROOTS):
        live, incoming, previous = addons / name, work / "incoming" / name, work / "previous" / name
        if previous.exists():
            if live.exists():
                move_placement(live, incoming)
            move_placement(previous, live)
        elif name not in state["existing"] and not incoming.exists() and live.exists():
            move_placement(live, incoming)


def clean_or_recover(addons, work):
    marker = work / "transaction.json"
    if marker.is_file():
        state = json.loads(marker.read_text(encoding="utf-8"))
        if not state["committed"]:
            if "files" in state:
                restore_update(addons, work, state)
            else:
                restore(addons, work, state)
    if work.exists():
        wait_for_placement(lambda: shutil.rmtree(work), work)


@contextmanager
def checked_archive(archive, progress=None):
    if not archive.is_file():
        raise ValueError("Put DMSpeaks-WoW.zip next to setup to install or repair the full add-on.")
    with archive.open("rb") as stream:
        total = os.fstat(stream.fileno()).st_size
        checksum = sha256()
        while block := stream.read(4 * 1024 * 1024):
            checksum.update(block)
            if progress:
                progress(stream.tell(), total)
        if checksum.hexdigest() != ARCHIVE_SHA256:
            raise ValueError("DMSpeaks-WoW.zip does not match this beta setup.")
        stream.seek(0)
        with ZipFile(stream) as source:
            entries = [(info, PurePosixPath(info.filename)) for info in source.infolist()]
            if ({path.parts[0] for _, path in entries if path.parts} != set(ROOTS)
                    or len({path.as_posix().casefold() for _, path in entries}) != len(entries)):
                raise ValueError("The zip does not contain the expected add-on folders.")
            for info, path in entries:
                if (info.is_dir() or info.filename != path.as_posix() or path.is_absolute()
                        or len(path.parts) < 2 or ".." in path.parts
                        or any("\\" in part or ":" in part for part in path.parts)):
                    raise ValueError("The zip contains an invalid add-on path.")
                if os.name == "nt" and hasattr(os.path, "isreserved") and os.path.isreserved(info.filename):
                    raise ValueError("The zip contains an invalid Windows file name.")
            member_names = {path.as_posix() for _, path in entries}
            required = {f"{name}/{name}.toc" for name in ROOTS} - RETIRED_MANIFESTS.keys()
            if not required.issubset(member_names) or RETIRED_MANIFESTS.keys() & member_names:
                raise ValueError("The zip is missing an add-on manifest.")
            yield source, entries


def placement_worker_count(limit=4):
    return min(limit, getattr(os, "process_cpu_count", os.cpu_count)() or 1)


def extract_archive(source, entries, incoming, progress=None):
    total = len(entries)
    workers = placement_worker_count()
    if progress:
        progress(0, total)
    if workers == 1:
        for index, (info, _) in enumerate(entries, 1):
            source.extract(info, incoming)
            if progress:
                progress(index, total)
        return

    # checked_archive has verified the entire ZIP. Create every parent once in
    # the empty staging area so file writers never race to create folders.
    folders = set()
    for _, relative in entries:
        folder = relative.parent
        if folder not in folders:
            (incoming / folder).mkdir(parents=True, exist_ok=True)
            folders.add(folder)
    pending = iter(entries)
    completed = 0

    def extract_group(group, mode="xb"):
        for info, relative in group:
            with source.open(info) as compressed, (incoming / relative).open(mode) as placed:
                shutil.copyfileobj(compressed, placed)
        return len(group)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        pool = executor
        while batch := list(islice(pending, workers * 64)):
            groups = [batch[start:start + 64] for start in range(0, len(batch), 64)]
            if pool is not None:
                try:
                    futures = [pool.submit(extract_group, group) for group in groups]
                except RuntimeError:
                    # Finish any running writers before replaying this batch in staging.
                    pool.shutdown(wait=True, cancel_futures=True)
                    pool = None
            counts = ([future.result() for future in futures] if pool is not None
                      else (extract_group(group, "wb") for group in groups))
            for count in counts:
                completed += count
                if progress:
                    progress(completed, total)


def matches_archive(path, info, size):
    if size != info.file_size:
        return False
    checksum = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            checksum = crc32(block, checksum)
    return checksum == info.CRC


def ensure_regular_folder(path, addons, checked, missing):
    if path in checked:
        return
    ensure_regular_folder(path.parent, addons, checked, missing)
    if is_link(path) or (path.exists() and not path.is_dir()):
        raise ValueError(f"The add-on path is not a regular folder: {path}")
    if not path.exists():
        missing.append(path.relative_to(addons).as_posix())
    checked.add(path)


def missing_branch(folder, addons, cache):
    """Find the first absent directory so a whole new branch can be placed together."""
    if folder in cache:
        return cache[folder]
    branch = missing_branch(folder.parent, addons, cache)
    if branch is None:
        try:
            info = folder.lstat()
        except FileNotFoundError:
            branch = folder.relative_to(addons).as_posix()
        else:
            if not S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"The add-on path is not a regular folder: {folder}")
    cache[folder] = branch
    return branch


def stage_update(addons, work, source, entries, *, group_folders=False, progress=None):
    incoming, previous = work / "incoming", work / "previous"
    changed, new_dirs = [], []
    trees = set()
    folders = {addons: None}
    checked_folders = {addons}
    total = len(entries)
    if progress:
        progress(0, total)
    for index, (info, relative) in enumerate(entries, 1):
        dest = addons / relative
        if group_folders:
            branch = missing_branch(dest.parent, addons, folders)
        else:
            ensure_regular_folder(dest.parent, addons, checked_folders, new_dirs)
            branch = None
        if branch:
            source.extract(info, incoming)
            trees.add(branch)
        else:
            try:
                stat = dest.lstat()
            except FileNotFoundError:
                stat = None
            if stat and (not S_ISREG(stat.st_mode) or
                         getattr(stat, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError(f"The add-on path is not a regular file: {dest}")
            if not stat or not matches_archive(dest, info, stat.st_size):
                source.extract(info, incoming)
                if stat:
                    backup = previous / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(dest, backup)
                changed.append(relative.as_posix())
        if progress:
            progress(index, total)
    return {"files": changed, "trees": sorted(trees), "new_dirs": new_dirs, "committed": False}



def stock_inventory():
    """Only paths and byte identities from verified distributions establish stock ownership."""
    path = Path(__file__).with_name("stock-files.json.gz")
    with path.open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != STOCK_SHA256:
            raise ValueError("The stock voice inventory is damaged. Run full Setup to restore maintenance.")
        stream.seek(0)
        with GzipFile(fileobj=stream) as contents:
            return json.load(contents)


def generated_status_files(addons, stock):
    """Recognize the exact companion formats, including older versions without profiles."""
    status = addons / "DMSpeaksLiteStatus"
    plain_path(status)
    plain_path(addons / "DMSpeaksLite")
    toc = status / "DMSpeaksLiteStatus.toc"
    snap = status / "Snapshot.lua"
    profiles = status / "Profiles.lua"
    content = status / "Content.lua"
    icon = status / "Icons/dmspeaks-mark.tga"
    progress_icon = status / "Icons/fetch-status.tga"
    plain_path(icon.parent)
    owned = []
    toc_prefix = ("## Interface: 16001\n## Title: DMSpeaks Fetch Status\n")
    old_notes = "## Notes: Progress written by the DMSpeaks desktop companion.\n"
    new_notes = ("## Notes: Lets WoW read fetch progress and find the name clips Lite has saved. Keep enabled.\n"
                 "## IconTexture: Interface\\AddOns\\DMSpeaksLiteStatus\\Icons\\dmspeaks-mark.tga\n")
    formats = {(toc_prefix + note + "Snapshot.lua\nNames.lua\n" + tail).encode()
               for note, tail in ((old_notes, ""), (new_notes, ""), (new_notes, "Profiles.lua\n"),
                                  (new_notes, "Profiles.lua\nContent.lua\n"))}
    if plain_path(toc) and toc.stat().st_size < 1024:
        raw = toc.read_bytes()
        # Strip only the generated version line and compare every other byte.
        normalized, version_count = re.subn(rb"(?<=## Title: DMSpeaks Fetch Status\n)## Version: 0\.1\.0-beta\.[0-9]{1,5}\n", b"", raw, count=1)
        current = (toc_prefix + new_notes.replace("dmspeaks-mark.tga", "fetch-status.tga")
                   + "Snapshot.lua\nNames.lua\nProfiles.lua\nContent.lua\n").encode()
        if raw in formats or (version_count == 1 and normalized == current):
            owned.append(toc)
    if plain_path(content) and content.stat().st_size < 512:
        lines = content.read_bytes().splitlines()
        if (lines[:1] == [b"DMSpeaksInstalledContent = {"] and lines[-1:] == [b"}"]
                and len(lines) >= 3 and re.fullmatch(rb"    checkedAt = [0-9]{1,12},", lines[1])):
            keys = (b"alliance", b"horde", b"shared", b"gossip", b"catalog")
            rows = [re.fullmatch(rb"    ([a-z]+) = (?:true|false),", line) for line in lines[2:-1]]
            if all(rows) and [row[1] for row in rows] == [key for key in keys if any(row[1] == key for row in rows)]:
                owned.append(content)
    if plain_path(icon):
        with icon.open("rb") as handle:
            if file_digest(handle, "sha256").hexdigest() in stock["DMSpeaksLite/Icons/dmspeaks-mark.tga"]:
                owned.append(icon)

    if plain_path(progress_icon):
        with progress_icon.open("rb") as handle:
            if file_digest(handle, "sha256").hexdigest() in stock["DMSpeaksLite/Icons/fetch-status.tga"]:
                owned.append(progress_icon)

    # Match the serializer's full output. A Lua file with additional user code is preserved.
    quoted = rb'"(?:[^"\\\x00-\x1f]|\\(?:["\\nrt]|[0-9]{3}))*"'
    field = rb"    ([a-zA-Z]+) = (?:" + quoted + rb"|[0-9]+),"
    keys = set(b"schema state ready total generatedAt requestId playerName playerRealm detail dialogueExcerpt "
               b"background liveHud settingsPort provider coverage npc stage npcNumber npcTotal dialogueNumber "
               b"nameOccurrence nameTotal instanceProgress etaSeconds".split())
    for snap, binding in ((snap, b"DMSpeaksLiteRuntimeSnapshot"), (addons / "DMSpeaksLite/Status.lua", b"addon.snapshot")):
        if not plain_path(snap) or snap.stat().st_size >= 65536:
            continue
        lines = snap.read_bytes().splitlines()
        if binding == b"addon.snapshot":
            if lines[:1] != [b"local _, addon = ..."]:
                continue
            lines = lines[1:]
        idle = [binding + b' = { schema = 1, state = "idle" }']
        if lines == idle:
            owned.append(snap)
        elif lines[:1] == [binding + b" = {"] and lines[-1:] == [b"}"]:
            matches = [re.fullmatch(field, line) for line in lines[1:-1]]
            if all(matches):
                fields = [match[1] for match in matches]
                if len(set(fields)) == len(fields) and {b"schema", b"state"} <= set(fields) <= keys:
                    owned.append(snap)
    if plain_path(profiles) and profiles.stat().st_size <= 4 * 1024 * 1024:
        lines = profiles.read_bytes().splitlines()
        row = rb"    \{id = " + quoted + rb", name = " + quoted + rb", realm = " + quoted + rb"\},"
        if (lines == [b"DMSpeaksLiteRoster = {}"] or
                (lines[:1] == [b"DMSpeaksLiteRoster = {"] and lines[-1:] == [b"}"]
                 and all(re.fullmatch(row, line) for line in lines[1:-1]))):
            owned.append(profiles)
    return owned


def generated_settings(path, hashes):
    """Normalize just the optional installer URL, then verify the entire original source."""
    from urllib.parse import urlsplit
    if path.stat().st_size > 1024 * 1024:
        return False
    content = path.read_bytes()
    field = re.compile(rb'^local liteInstallerURL = "(https://(?:[^"\\\r\n]|\\")*)"$', re.M)
    matches = list(field.finditer(content))
    if len(matches) != 1:
        return False
    try:
        url = matches[0][1].replace(rb'\"', b'"').decode()
        parsed = urlsplit(url)
        valid = parsed.hostname and parsed.username is None and len(url) <= 512
    except (ValueError, UnicodeError):
        return False
    return bool(valid and all(char.isprintable() and not char.isspace() and char != "\\" for char in url)
                and sha256(field.sub(b"local liteInstallerURL = nil", content)).hexdigest() in hashes)


def prune_empty(folders):
    for folder in sorted(set(folders), key=lambda path: len(path.parts), reverse=True):
        if plain_path(folder) and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


def installed_digest(addons, relative, checked, missing):
    path = addons / relative
    ensure_regular_folder(path.parent, addons, checked, missing)
    plain_path(path)
    try:
        if not S_ISREG(path.stat().st_mode):
            raise ValueError(f"The add-on path is not a regular file: {path}")
        with path.open("rb") as stream:
            return file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return None


@contextmanager
def installed_beta_update(addons, delta, progress=None):
    """Match the known release by its unchanged metadata and the files being replaced."""
    if not (addons / CATALOG).is_file():
        yield None
        return
    with delta.open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != DELTA_SHA256:
            raise ValueError("The update bundled in setup is damaged.")
        stream.seek(0)
        with ZipFile(stream) as source:
            manifest = json.loads(source.read("update.json"))
            checked, missing = {addons}, []
            catalog = addons / CATALOG
            # The two releases share the same plan archive. Its signed digest
            # sidecar is checked below and its presence and size here.
            if (not catalog.is_file() or catalog.stat().st_size != manifest["catalog_bytes"]
                    or any(installed_digest(addons, path, checked, missing) != digest
                           for path, digest in manifest["base"].items())):
                yield None
                return
            entries = []
            total = len(manifest["files"])
            for index, (path, (before, after)) in enumerate(manifest["files"].items(), 1):
                current = installed_digest(addons, path, checked, missing)
                accepted = before if isinstance(before, list) else [before]
                if current != after and current not in accepted:
                    yield None
                    return
                if current != after:
                    if sha256(source.read(path)).hexdigest() != after:
                        raise ValueError(f"The file bundled in setup is damaged: {path}")
                    entries.append((source.getinfo(path), PurePosixPath(path)))
                if progress:
                    progress(index, total)
            yield source, entries


def apply_update(addons, work, marker, state, progress=None):
    total = len(state.get("trees", ())) + len(state["files"])
    if progress:
        progress(0, total)
    write_marker(marker, state)
    for relative in state["new_dirs"]:
        (addons / relative).mkdir(exist_ok=True)
    for index, relative in enumerate(state.get("trees", ()), 1):
        dest = addons / relative
        if dest.exists() or is_link(dest):
            raise FileExistsError(f"A folder was created during installation: {dest}")
        move_placement(work / "incoming" / relative, dest)
        if progress:
            progress(index, total)
    for index, relative in enumerate(state["files"], len(state.get("trees", ())) + 1):
        try:
            move_placement(work / "incoming" / relative, addons / relative, replace=True)
        except PlacementInUse:
            raise
        except OSError as error:
            raise OSError(f"Could not update {relative}: {error.strerror or error}") from error
        if progress:
            progress(index, total)
    for relative in state.get("retired", ()):
        backup = work / "previous" / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        move_placement(addons / relative, backup, replace=True)
    write_marker(marker, {**state, "committed": True})


def restore_update(addons, work, state):
    for relative in state.get("retired", ()):
        backup = work / "previous" / relative
        if backup.exists():
            move_placement(backup, addons / relative, replace=True)
    for relative in reversed(state["files"]):
        if (work / "incoming" / relative).exists():
            continue
        backup, dest = work / "previous" / relative, addons / relative
        if backup.exists():
            recovery = work / "recovery" / relative
            recovery.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(backup, recovery)
            move_placement(recovery, dest, replace=True)
        else:
            wait_for_placement(lambda: dest.unlink(missing_ok=True), dest)
    for relative in reversed(state.get("trees", ())):
        staged = work / "incoming" / relative
        if not staged.exists():
            move_placement(addons / relative, staged)
    for relative in reversed(state["new_dirs"]):
        path = addons / relative
        if path.is_dir() and not any(path.iterdir()):
            wait_for_placement(path.rmdir, path)


def install(wow, archive, delta=None, *, repair=False, progress_window=0, desktop_complete=False,
            repair_cancel=None):
    print("Checking the WoW folder...", flush=True)
    start = 25 if desktop_complete else 0

    def phase(label, first, last, *, files=False):
        return stage_progress(progress_window, label, first, last, show_count=files and not repair,
                              detail_once=not repair)

    progress = phase("Installing WoW voices", 40, 92, files=True)
    finishing = phase("Finishing the WoW add-on", 92, 97)

    def repair_progress(report):
        def update(count, total):
            if repair_cancel is not None:
                repair_cancel()
            report(count, total)
        update(0, 1)
        return update

    addons = check_game(wow)
    if os.name == "nt":
        addons = Path(windows_api_path(addons))
        archive = Path(windows_api_path(archive))
    interface = addons.parent
    plain_path(interface)
    work = interface / WORK
    if is_link(work) or is_link(interface / LOCK) or is_link(addons):
        raise ValueError("Choose a WoW folder with a regular AddOns directory.")
    with placement_lock(interface / LOCK):
        clean_or_recover(addons, work)
        incoming, previous = work / "incoming", work / "previous"
        incoming.mkdir(parents=True)
        previous.mkdir()
        marker = work / "transaction.json"
        try:
            existing = any((addons / name).exists() or is_link(addons / name) for name in ROOTS)
            checked = {addons}
            retired = [path for path, hashes in stock_inventory()["retired"].items()
                       if installed_digest(addons, path, checked, []) in hashes]
            if existing and delta is not None and not repair:
                checking = phase("Checking the installed add-on", start, 32)
                checking(0, 1)
                with installed_beta_update(addons, delta, checking) as update:
                    if update is not None:
                        source, entries = update
                        if entries or retired:
                            updates = phase("Preparing WoW updates", 40, 92, files=True)
                            state = stage_update(addons, work, source, entries, group_folders=True, progress=updates)
                            state["retired"] = retired
                            apply_update(addons, work, marker, state, finishing)
                        else:
                            print("DMSpeaks add-on files are up to date.", flush=True)
                        return
                start = 32
            checking = phase("Checking the voice package", start, 40)
            if repair:
                checking = repair_progress(checking)
            else:
                checking(0, 1)
            with checked_archive(archive, checking) as (source, entries):
                if existing or retired:
                    if repair:
                        progress = repair_progress(stage_progress(progress_window, "Checking installed voices", 40, 92))
                        finishing = stage_progress(progress_window, "Installing WoW voices", 92, 97)
                    else:
                        progress = phase("Checking installed voice files", 40, 92, files=True)
                    state = stage_update(addons, work, source, entries, group_folders=True, progress=progress)
                    if repair_cancel is not None:
                        repair_cancel(before_apply=True)
                    state["retired"] = retired
                    apply_update(addons, work, marker, state, finishing)
                else:
                    if repair_cancel is not None:
                        repair_cancel(before_apply=True)
                    extract_archive(source, entries, incoming, progress)
                    state = {"existing": [], "committed": False}
                    finishing(0, len(ROOTS))
                    write_marker(marker, state)
                    for index, name in enumerate(ROOTS, 1):
                        move_placement(incoming / name, addons / name)
                        finishing(index, len(ROOTS))
                    write_marker(marker, {**state, "committed": True})
        finally:
            clean_or_recover(addons, work)


def preflight_catalog_removal(addons, checked, absent):
    catalog = addons / CATALOG
    ensure_regular_folder(catalog.parent, addons, checked, absent)
    try:
        info = catalog.lstat()
    except FileNotFoundError:
        return
    attributes = getattr(info, "st_file_attributes", 0)
    if not S_ISREG(info.st_mode) or attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"The dialogue catalog is not a regular file: {CATALOG}")
    if os.name != "nt":
        return
    if attributes & FILE_ATTRIBUTE_READONLY:
        raise ValueError(f"The dialogue catalog is read-only: {CATALOG}")

    from ctypes import WinDLL, WinError, c_void_p, get_last_error, sizeof
    from ctypes import wintypes

    kernel = WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create_file.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    # Request DELETE access without deleting or modifying the file. Windows
    # rejects this while another handle has the catalog open without delete sharing.
    handle = create_file(str(catalog), 0x10000, 0x7, None, 3, 0x00200000, None)
    if handle == (1 << (8 * sizeof(c_void_p))) - 1:
        error = get_last_error()
        if error in (2, 3):
            return
        if error in (32, 33):
            raise CatalogInUse("The dialogue catalog is in use. Close WoW, sign out of Windows to stop Lite, "
                               "then retry Uninstall before reopening either app.")
        raise OSError(f"Could not check {CATALOG} for removal: {WinError(error)}")
    kernel.CloseHandle(handle)


def stage_progress(hwnd, label, start=0, end=100, *, show_count=False, detail_once=False):
    """Map completed units onto the operation's share of the wizard's progress."""
    last = -1
    if hwnd and os.name == "nt":
        from ctypes import WinDLL, wintypes, c_wchar_p, c_void_p, cast
        user = WinDLL("user32")
        user.GetParent.argtypes = (wintypes.HWND,)
        user.GetParent.restype = wintypes.HWND
        user.GetDlgItem.argtypes = (wintypes.HWND, wintypes.INT)
        user.GetDlgItem.restype = wintypes.HWND
        status = user.GetDlgItem(user.GetParent(hwnd), 1906)
        # SetWindowText cannot change a control in another process. WM_SETTEXT
        # is marshalled by Windows, and a timeout keeps placement independent of the UI.
        user.SendMessageTimeoutW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                             c_void_p, wintypes.UINT, wintypes.UINT, wintypes.LPVOID)

    def report(count, total):
        nonlocal last
        percent = 100 * count // max(1, total)
        if percent == last:
            return
        count_text = f"  {count:,} of {total:,}" if show_count else ""
        if hwnd and os.name == "nt":
            overall = start + (end - start) * percent // 100
            if show_count or last == -1 or overall != start + (end - start) * last // 100:
                user.SendMessageTimeoutW(hwnd, 0x402, overall, None, 2, 200, None)  # PBM_SETPOS
                if status:
                    text = c_wchar_p(f"{label}{count_text}  ({overall}% overall)" if show_count
                                     else f"{label}  {overall}% overall")
                    user.SendMessageTimeoutW(status, 0x0C, 0, cast(text, c_void_p), 2, 200, None)
        if detail_once:
            if last == -1:
                print(label, flush=True)
        elif percent // 10 != last // 10:
            print(f"{label}{count_text}" if show_count else f"{label}  {percent}%", flush=True)
        last = percent

    return report


def repair_window_cancellation(button):
    """Poll the wizard's one-way Cancel button, then close it before applying changes."""
    if not button:
        return None
    if os.name != "nt" or button < 0:
        raise ValueError("Invalid Windows repair cancel control.")
    from ctypes import WinDLL, byref, c_size_t
    from ctypes import wintypes

    window = WinDLL("user32")
    window.EnableWindow.argtypes = (wintypes.HWND, wintypes.BOOL)
    window.SendMessageTimeoutW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                          wintypes.UINT, wintypes.UINT, wintypes.LPVOID)
    window.SendMessageTimeoutW.restype = wintypes.LPARAM
    next_poll = 0.0

    def check(*, before_apply=False):
        nonlocal next_poll
        now = monotonic()
        if before_apply:
            window.EnableWindow(button, False)
        elif now < next_poll:
            return
        next_poll = now + 0.12
        checked = c_size_t()
        # BM_GETCHECK is delivered on the button's UI thread. Disabling the button
        # before the final read resolves a click at the end of the scan.
        read = window.SendMessageTimeoutW(button, 0x00F0, 0, 0, 0x02, 1000, byref(checked))
        if not read or checked.value:
            window.EnableWindow(button, False)
            raise RepairCancelled

    return check


def uninstall_file_identity(target, relative):
    info = target.lstat()
    if (not S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(f"The add-on path is not a regular file: {relative}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def verify_uninstall_file(target, relative, hashes):
    try:
        identity = uninstall_file_identity(target, relative)
        with target.open("rb") as handle:
            digest = file_digest(handle, "sha256").hexdigest()
        if digest in hashes or (relative == "DMSpeaksLite/Settings.lua" and generated_settings(target, hashes)):
            return identity
    except FileNotFoundError:
        pass
    except OSError as error:
        raise OSError(f"Could not check {relative}: {error.strerror or error}") from error
    return None


def unlink_verified_file(target, relative, identity, *, recheck=True):
    if identity is None:
        return
    try:
        if not recheck or uninstall_file_identity(target, relative) == identity:
            target.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise OSError(f"Could not remove {relative}: {error.strerror or error}") from error


def windows_stock_remover():
    """Read and remove exact stock files through one Windows handle per file."""
    from ctypes import WinDLL, WinError, byref, c_ubyte, c_void_p, create_string_buffer, get_last_error, sizeof
    from ctypes import wintypes as wt

    api = WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = (wt.LPCWSTR, wt.DWORD, wt.DWORD, c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE)
    api.CreateFileW.restype = wt.HANDLE
    api.GetFileInformationByHandleEx.argtypes = (wt.HANDLE, wt.INT, c_void_p, wt.DWORD)
    api.ReadFile.argtypes = (wt.HANDLE, c_void_p, wt.DWORD, c_void_p, c_void_p)
    api.SetFileInformationByHandle.argtypes = (wt.HANDLE, wt.INT, c_void_p, wt.DWORD)
    api.CloseHandle.argtypes = (wt.HANDLE,)
    invalid = c_void_p(-1).value

    def remove_group(checks):
        fallback = []
        buffer = create_string_buffer(256 * 1024)
        data = memoryview(buffer).cast("B")
        count = wt.DWORD()
        attribute_tag = (wt.DWORD * 2)()
        dispose = c_ubyte(1)
        for check in checks:
            target, relative, hashes = check
            if relative == "DMSpeaksLite/Settings.lua":
                fallback.append(check)
                continue
            # GENERIC_READ | DELETE, share reads only, OPEN_EXISTING, no link following.
            handle = api.CreateFileW(str(target), 0x80010000, 1, None, 3, 0x00200000, None)
            if handle == invalid:
                code = get_last_error()
                if code in (2, 3):
                    continue
                if code in (5, 32, 33):
                    fallback.append(check)
                    continue
                raise WinError(code, f"Could not open {relative}")
            try:
                # FileAttributeTagInfo provides the flags without reopening the path.
                if not api.GetFileInformationByHandleEx(handle, 9, attribute_tag, sizeof(attribute_tag)):
                    raise WinError(get_last_error(), f"Could not inspect {relative}")
                if attribute_tag[0] & (0x10 | FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ValueError(f"The add-on path is not a regular file: {relative}")
                digest = sha256()
                while True:
                    if not api.ReadFile(handle, buffer, len(buffer), byref(count), None):
                        raise WinError(get_last_error(), f"Could not read {relative}")
                    if not count.value:
                        break
                    digest.update(data[:count.value])
                if digest.hexdigest() in hashes and not api.SetFileInformationByHandle(handle, 4, byref(dispose), 1):
                    code = get_last_error()
                    if code in (5, 32, 33):
                        fallback.append(check)
                    else:
                        raise WinError(code, f"Could not remove {relative}")
            finally:
                api.CloseHandle(handle)
        return fallback

    return remove_group


def remove_uninstall_batch(checks, pool, worker_count, windows_remove):
    if windows_remove is not None:
        # A hard-linked file may be busy in another group. Run ordinary checks
        # for contested paths only after every native handle has closed.
        if pool is not None:
            group_size = (len(checks) + worker_count - 1) // worker_count
            try:
                tasks = [pool.submit(windows_remove, checks[i:i + group_size])
                         for i in range(0, len(checks), group_size)]
            except RuntimeError:
                pool.shutdown(wait=True, cancel_futures=True)
                pool = None
        fallback = ([check for task in tasks for check in task.result()] if pool is not None
                    else windows_remove(checks))
        for target, relative, hashes in fallback:
            unlink_verified_file(target, relative, verify_uninstall_file(target, relative, hashes))
        return pool

    if pool is not None:
        try:
            tasks = [pool.submit(verify_uninstall_file, *check) for check in checks]
        except RuntimeError:
            pool.shutdown(wait=True, cancel_futures=True)
            pool = None
    identities = ([task.result() for task in tasks] if pool is not None
                  else (verify_uninstall_file(*check) for check in checks))
    for (target, relative, _), identity in zip(checks, identities):
        unlink_verified_file(target, relative, identity, recheck=pool is not None)
    return pool


def uninstall(wow, ownership, progress_window=0):
    """Remove the shipped add-on files and prune only empty directories."""
    addons = wow / "Interface/AddOns"
    if os.name == "nt":
        addons = Path(windows_api_path(addons))
    if not addons.exists():
        return
    interface = addons.parent
    plain_path(interface)
    work = interface / WORK
    for lockfile in (addons / ".DMSpeaksLite-setup.lock", interface / LOCK):
        plain_path(lockfile)
    if is_link(work) or is_link(interface / LOCK) or is_link(addons) or not addons.is_dir():
        raise ValueError("The WoW add-on folder or installer workspace is linked.")
    print("Checking the installed WoW file list...", flush=True)
    inventory = stock_inventory()
    with ownership.open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != OWNERS_SHA256:
            raise ValueError("The installed DMSpeaks file list is damaged.")
        stream.seek(0)
        with GzipFile(fileobj=stream) as contents:
            files = [PurePosixPath(name) for name in contents.read().decode("utf-8").splitlines()]
    if not inventory["current"].keys() <= set(map(str, files)) <= (inventory["current"].keys() | inventory["retired"].keys()):
        raise ValueError("The installed file list disagrees with the stock inventory.")
    candidates = {**inventory["current"], **inventory["retired"]}
    files = [PurePosixPath(name) for name in candidates]
    with placement_lock(interface / LOCK), placement_lock(addons / ".DMSpeaksLite-setup.lock"):
        clean_or_recover(addons, work)
        generated = generated_status_files(addons, inventory["current"])
        checked, absent = {addons}, []
        show_progress = stage_progress(progress_window, "Checking and removing WoW files", 0, 85,
                                       show_count=True, detail_once=True)
        total = len(files)
        print("Checking whether the dialogue catalog can be removed...", flush=True)
        if CATALOG in files:
            preflight_catalog_removal(addons, checked, absent)
        show_progress(0, total)
        last_update = monotonic()
        pending = iter(files)
        removed_or_checked = 0
        windows_remove = windows_stock_remover() if os.name == "nt" else None
        cpu_count = placement_worker_count(12 if windows_remove is not None else 4)
        with ExitStack() as executor:
            workers = executor.enter_context(ThreadPoolExecutor(max_workers=cpu_count)) if cpu_count > 1 else None
            while batch := list(islice(pending, 128)):
                checks = []
                for relative in batch:
                    target = addons / relative
                    ensure_regular_folder(target.parent, addons, checked, absent)
                    name = relative.as_posix()
                    checks.append((target, name, candidates[name]))
                workers = remove_uninstall_batch(checks, workers, cpu_count, windows_remove)
                removed_or_checked += len(batch)
                if removed_or_checked == total or monotonic() - last_update >= 0.65:
                    show_progress(removed_or_checked, total)
                    last_update = monotonic()
        for target in generated:
            target.unlink(missing_ok=True)
        prune_empty([addons / "DMSpeaksLiteStatus/Icons", addons / "DMSpeaksLiteStatus",
                     interface / ".dmspeaks-spoken-install-20261104"])
        folders = sorted(checked - {addons}, key=lambda path: len(path.parts), reverse=True)
        prune = stage_progress(progress_window, "Removing empty folders", 85, 92, detail_once=True)
        prune(0, len(folders))
        for index, folder in enumerate(folders, 1):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
            prune(index, len(folders))


def remove_idle_marker(path):
    """Delete the known sentinel through the opened file, refusing active Windows handles."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (not S_ISREG(info.st_mode) or info.st_size > 1
            or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT):
        return
    if os.name != "nt":
        import fcntl
        with path.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.path.samestat(os.fstat(handle.fileno()), path.lstat()) and handle.read(2) in (b"", b"\0"):
                path.unlink()
        return

    import ctypes
    from ctypes import wintypes
    import msvcrt
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    open_file = api.CreateFileW
    open_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    open_file.restype = wintypes.HANDLE
    # GENERIC_READ | DELETE, no sharing, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT.
    handle = open_file(str(path), 0x80010000, 0, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        if code in (2, 3):
            return
        raise OSError(f"Close Lite before removing {path.name}: {ctypes.WinError(code)}")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        api.CloseHandle(wintypes.HANDLE(handle))
        raise
    with os.fdopen(descriptor, "rb") as stream:
        if (not os.path.samestat(os.fstat(stream.fileno()), path.lstat())
                or stream.read(2) not in (b"", b"\0")):
            return
        dispose = api.SetFileInformationByHandle
        dispose.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        dispose.restype = wintypes.BOOL
        delete = ctypes.c_ubyte(1)
        if not dispose(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):  # FileDispositionInfo
            raise ctypes.WinError(ctypes.get_last_error())


def remove_game_markers(wow):
    interface, addons = wow / "Interface", wow / "Interface/AddOns"
    for parent in (wow, interface, addons):
        plain_path(parent)
    markers = [interface / LOCK]
    markers.extend(addons / name for name in (".dmspeaks-automatic-launch.lock", ".dmspeaks-game-ingress.lock",
                                               ".dmspeaks-name-overlay.lock", ".DMSpeaksLite-setup.lock",
                                               ".DMSpeaksSpokenNames.lock"))
    for marker in markers:
        remove_idle_marker(marker)




def plain_path(path):
    """Ignore missing paths, and refuse to follow redirects when clearing personal data."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"Cannot clear a linked path: {path}")
    if not (S_ISDIR(info.st_mode) or S_ISREG(info.st_mode)):
        raise ValueError(f"Cannot clear a linked or special file: {path}")
    return info


def audio_cleanup(folder, files, directories, *, all_files):
    for path in folder.iterdir():
        info = plain_path(path)
        if info is None:
            continue
        if S_ISDIR(info.st_mode):
            audio_cleanup(path, files, directories, all_files=all_files)
        elif all_files or path.suffix.lower() == ".ogg":
            files.append(path)
    directories.append(folder)


def pending_name_sync(database, key):
    if not plain_path(database):
        return False
    for suffix in ("-wal", "-shm", "-journal"):
        plain_path(database.with_name(database.name + suffix))
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
            return False
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return bool(row and json.loads(row[0]))


@contextmanager
def finish_name_handoff(wow, data):
    """Publish an interrupted owner's recordings before its local identity can be removed."""
    database = data / "studio.sqlite"
    fetch_key = "lite_name_card_sync_" + str(wow) + "_fetch"
    plain_path(data)
    addons = wow / "Interface/AddOns"
    lock = addons / ".dmspeaks-game-ingress.lock"
    for path in (wow, addons.parent, addons, lock):
        plain_path(path)
    with ExitStack() as stack:
        try:
            # Hold the same kernel lock that GameIngress.start takes, including
            # the time spent removing files. A newly opened Lite cannot race us.
            if addons.is_dir():
                stack.enter_context(placement_lock(lock))
            if pending_name_sync(database, fetch_key):
                settle_reserved_names(wow, data, fetch_key)
        except ImportError as error:
            raise NameSyncPending("Restore Lite with full Setup and reconnect name storage before retrying uninstall.") from error
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            raise NameSyncPending(f"Shared name audio is waiting to sync. {error}") from error
        yield


def settle_reserved_names(wow, data, fetch_key):
    # The packaged interpreter is in Builds/<build>/runtime/python. Load the
    # installed sync code only for an actual reservation, without starting a
    # game watcher, background listener, speech provider or name fetch.
    if os.name == "nt":
        package = Path(sys.executable).resolve().parents[2]
        if package.parent != Path(os.environ["LOCALAPPDATA"]) / "DMSpeaksLite/Builds":
            raise NameSyncPending("Restore Lite with full Setup and reconnect name storage before retrying uninstall.")
        sys.path.insert(0, str(package / "source"))
    from lite_game_ingress import GameIngress
    from lite_share_playback import check_folder
    from settings_store import SettingsStore

    with closing(SettingsStore(data)) as store:
        saved = store.setting(fetch_key, {})
        if not saved:
            return
        if store.setting("lite_game_fetch_inflight", {}):
            raise ValueError("Open Lite to finish or pause the active fetch before uninstalling.")
        ingress = GameIngress(SimpleNamespace(folder=data, store=store), wow, None, store_managed=True)
        config = store.setting(ingress.cards.sync_key, {})
        check_folder(config)
        result = ingress.cards.synchronize()
        if result.get("state") != "synced" or store.setting(fetch_key, {}):
            raise ValueError(result.get("error") or "The shared fetch is still reserved.")
        try:
            check_folder(config)
        except (OSError, ValueError):
            # If the drive disappeared during release, retry the same handoff.
            # A completed release can be repeated without touching another owner.
            store.set_setting(fetch_key, saved)
            raise


def forget_coverage(db, wow, *, shared_cache=False):
    prefix = "lite_coverage_"
    discard = []
    for key, in db.execute("SELECT key FROM settings WHERE key GLOB 'lite_coverage_*'"):
        scope = json.loads(key[len(prefix):])
        if isinstance(scope, list) and scope and Path(scope[0]) == wow:
            discard.append((key,))
    for key, value in db.execute("SELECT key, value FROM settings WHERE key IN (?, ?)",
                                 ("lite_game_last_fetch", "lite_game_fetch_inflight")):
        if Path(json.loads(value).get("root", "")) == wow:
            discard.append((key,))
    # The older companion's placement record points into its local name cache.
    # Removing personal audio also disconnects this installation from its backup.
    # The remote library belongs to the user and can be restored on another PC.
    sync = "lite_name_card_sync_" + str(wow)
    if not shared_cache:
        discard.append(("spoken_paced_deliveries",))
    discard.extend([ ("lite_wow_profiles_" + str(wow),),
                    ("lite_wow_accounts_" + str(wow),), (sync,), (sync + "_receipt",), (sync + "_fetch",)])
    db.executemany("DELETE FROM settings WHERE key = ?", discard)


def remove_saved_names(wow, data, progress_window=0, *, shared_cache=False):
    """Clear the selected game's installed name audio and this user's Lite name cache."""
    addons = wow / "Interface" / "AddOns"
    status = addons / "DMSpeaksLiteStatus"
    cache = data / "spoken-player-names"
    for path in (wow, addons.parent, addons, status, status / "generated", status / "Icons",
                 data, cache):
        plain_path(path)
    files, directories = [], []
    with ExitStack() as stack:
        for folder, lockname in ((addons, ".dmspeaks-name-overlay.lock"),
                                 (cache, ".name-processing.lock")):
            if folder.is_dir():
                lock = folder / lockname
                plain_path(lock)
                stack.enter_context(placement_lock(lock))
        index = status / "Names.json"
        loader = status / "Names.lua"
        if plain_path(index):
            source = Path(sys.executable).resolve().parents[2] / "source"
            if os.name == "nt" and source.parent.parent == Path(os.environ["LOCALAPPDATA"]) / "DMSpeaksLite/Builds":
                sys.path.insert(0, str(source))
            from lite_store_playback import lua_index, name_audio_relative, saved_names
            entries = saved_names(addons)
            expected_lua = lua_index(entries)
            for choices in entries.values():
                for clip in choices.values():
                    for item in (clip, clip.get("prefix")):
                        if item is None:
                            continue
                        relative = name_audio_relative(item)
                        path = addons / relative
                        for parent in relative.parents:
                            plain_path(addons / parent)
                        if plain_path(path) and path.is_file():
                            with path.open("rb") as stream:
                                if file_digest(stream, "sha256").hexdigest() == item["sha256"]:
                                    files.append(path)
                        directories.extend(parent for parent in path.parents if parent.is_relative_to(status))
            files.append(index)
            if plain_path(loader) and loader.read_bytes() in (expected_lua, b"DMSpeaksFetchedNames = {}\n"):
                files.append(loader)
        elif plain_path(loader) and loader.read_bytes() in (b"DMSpeaksFetchedNames = {}\n", b"DMSpeaksFetchedNames = {\n}\n"):
            files.append(loader)
        probes = status / "SharePlayback"
        if plain_path(probes):
            for item in probes.iterdir():
                if item.name == "shared.ogg" or re.fullmatch(r"ready-[0-9]+\.ogg", item.name):
                    if plain_path(item):
                        files.append(item)
            directories.append(probes)
        directories.extend([status / "Icons", status / "generated", status])
        if not shared_cache and cache.is_dir():
            managed = {"requests", "packages", "paced-packages", "paced-active", "paced-units",
                       "paced-generated", "expanded-generated", "DMSpeaksSpokenNames.zip"}
            scratch = (".placement-", ".stock-", ".name-overlay-", ".paced-name-", ".expanded-name-")
            for child in cache.iterdir():
                if child.name not in managed and not child.name.startswith(scratch):
                    continue
                info = plain_path(child)
                if info and S_ISDIR(info.st_mode):
                    audio_cleanup(child, files, directories, all_files=True)
                elif info:
                    files.append(child)
        database = data / "studio.sqlite"
        db = None
        if plain_path(database):
            for suffix in ("-wal", "-shm", "-journal"):
                plain_path(database.with_name(database.name + suffix))
            db = stack.enter_context(closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)))
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
                forget_coverage(db, wow, shared_cache=shared_cache)
        progress = stage_progress(progress_window, "Removing saved names", 92, 96)
        for index, file in enumerate(files, 1):
            file.unlink(missing_ok=True)
            progress(index, len(files))
        prune_empty(directories)
        if db is not None:
            db.commit()


if __name__ == "__main__":
    try:
        parser = ArgumentParser(description=__doc__)
        parser.add_argument("wow_folder", type=Path)
        parser.add_argument("addon_archive", type=Path)
        parser.add_argument("update_archive", type=Path, nargs="?")
        parser.add_argument("--progress-window", type=int, default=0, help=SUPPRESS)
        parser.add_argument("--repair-cancel-window", type=int, default=0, help=SUPPRESS)
        parser.add_argument("--shared-cache", action="store_true", help=SUPPRESS)
        parser.add_argument("--desktop-complete", action="store_true", help=SUPPRESS)
        operation = parser.add_mutually_exclusive_group()
        operation.add_argument("--remove-game-markers", action="store_true", help=SUPPRESS)
        operation.add_argument("--repair", action="store_true")
        operation.add_argument("--remove-saved-names", action="store_true", help=SUPPRESS)
        operation.add_argument("--uninstall", type=Path, metavar="INSTALLED_FILE_LIST")
        args = parser.parse_args()
        if args.repair_cancel_window and not args.repair:
            parser.error("Repair cancellation requires --repair.")
        wow = args.wow_folder.resolve()
        if args.remove_game_markers:
            remove_game_markers(wow)
        elif args.remove_saved_names:
            if os.name != "nt":
                raise ValueError("Use Windows maintenance to remove saved name audio.")
            data = Path(os.environ["LOCALAPPDATA"]) / "DMSpeaksLiteBeta"
            with finish_name_handoff(wow, data):
                remove_saved_names(wow, data, args.progress_window, shared_cache=args.shared_cache)
        elif args.uninstall is not None:
            with ExitStack() as stack:
                if os.name == "nt":
                    data = Path(os.environ["LOCALAPPDATA"]) / "DMSpeaksLiteBeta"
                    stack.enter_context(finish_name_handoff(wow, data))
                uninstall(wow, args.uninstall.resolve(), args.progress_window)
            print("DMSpeaks add-on files removed.", flush=True)
        else:
            delta = args.update_archive.resolve() if args.update_archive else None
            install(wow, args.addon_archive.resolve(), delta, repair=args.repair,
                    progress_window=args.progress_window, desktop_complete=args.desktop_complete,
                    repair_cancel=repair_window_cancellation(args.repair_cancel_window))
            print("DMSpeaks add-on and audio installed.", flush=True)
    except RepairCancelled:
        print("Repair canceled. No staged files were applied.", flush=True)
        sys.exit(4)
    except PlacementInUse as error:
        print(str(error), flush=True)
        sys.exit(5)
    except CatalogInUse as error:
        print(str(error), flush=True)
        sys.exit(2)
    except NameSyncPending as error:
        print(str(error), flush=True)
        sys.exit(3)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(str(error), flush=True)
        sys.exit(1)
