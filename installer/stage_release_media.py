"""Verify and prepare the content downloads for Windows Lite Setup."""

import argparse
from hashlib import file_digest, sha256
import json
from os import getpid
from pathlib import Path
import re
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from urllib.parse import quote
from urllib.request import urlopen
from zipfile import BadZipFile, ZipFile


GAME = "DMSpeaks-WoW.zip"
REFERENCES = "DMSpeaks-Lite-Name-Data.zip"
MEMBERS = ("embedded-catalog.zip", "prefix-data.zip")
COLLECTIONS = ("Alliance", "Horde", "Shared", "Gossip")
PACK_MASKS = range(1 << len(COLLECTIONS))


def playback_zip(collection):
    return f"DMSpeaks-Lite-{collection}-Audio.zip"


def matches(path, record):
    if not path.is_file() or path.is_symlink() or path.stat().st_size != record["bytes"]:
        return False
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest() == record["sha256"]


def pending_download_prefix(pid):
    """Identify temporary downloads belonging to one staging process."""
    return f".download-{pid}-"


def download(url, target, record, progress=None):
    """Stream, verify, and atomically publish a release archive."""
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with NamedTemporaryFile(dir=target.parent, prefix=pending_download_prefix(getpid()),
                                suffix=".part", delete=False) as output:
            pending = Path(output.name)
            with urlopen(url, timeout=30) as response:
                if not response.geturl().startswith("https://"):
                    raise ValueError("The release download must use HTTPS.")
                checksum = sha256()
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > record["bytes"]:
                        raise ValueError(f"{target.name} exceeds the size recorded for this release.")
                    checksum.update(chunk)
                    output.write(chunk)
                    if progress:
                        progress(size)
            if size != record["bytes"] or checksum.hexdigest() != record["sha256"]:
                raise ValueError(f"{target.name} does not match this release.")
        pending.replace(target)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def obtain(name, selected, cache, release, progress=None):
    """Check adjacent files and verified caches from other builds before downloading."""
    record = release["files"][name]
    cached = cache / name
    if progress:
        progress("checking", 0)
    if selected.exists():
        if not matches(selected, record):
            raise ValueError(f"{selected.name} does not match this release.")
        return selected
    if matches(cached, record):
        return cached
    if re.fullmatch(r"[a-f0-9]{64}", cache.name):
        # Setup revisions change even when their media is byte-for-byte identical.
        for directory in cache.parent.glob("*"):
            if (directory == cache or not re.fullmatch(r"[a-f0-9]{64}", directory.name)
                    or directory.is_symlink() or not directory.is_dir()):
                continue
            candidate = directory / name
            if matches(candidate, record):
                return candidate
    base = release.get("download_base")
    if not base:
        raise ValueError(f"Put {name} beside Setup.")
    if not re.fullmatch(r"https://github\.com/mwilckenGitHub/DMSpeaks-Lite/releases/download/[A-Za-z0-9._-]+", base):
        raise ValueError("The Lite release download address is invalid.")
    print(f"Downloading {name}...", flush=True)
    if progress:
        progress("downloading", 0)
    download(base + "/" + quote(name), cached, record,
             (lambda size: progress("downloading", size)) if progress else None)
    return cached


def installed_collections(addons, prefix):
    return {name for name in COLLECTIONS
            if (addons / (prefix + name) / (prefix + name + ".toc")).is_file()}


def select_collections(addons, pack_mask, include_installed):
    if pack_mask is None:
        selected = installed_collections(addons, "SpokenQuestsAudio")
        if not selected:
            raise ValueError("Choose the Lite voice packs in Setup, or specify /VOICEPACKS=0 for a silent install without optional packs.")
    else:
        if pack_mask not in PACK_MASKS:
            raise ValueError("The Lite voice pack selection is invalid.")
        selected = {name for index, name in enumerate(COLLECTIONS) if pack_mask & (1 << index)}
    if include_installed:
        selected.update(installed_collections(addons, "DMSpeaksPersonalized"))
    return [name for name in COLLECTIONS if name in selected]


def stage(game, wow, cache, work, release, *, pack_mask=None, include_installed=False, progress=None):
    """Prepare and verify the common archives and the selected voice packs."""
    addons = wow / "Interface" / "AddOns"
    collections = select_collections(addons, pack_mask, include_installed)
    pack_names = [playback_zip(collection) for collection in collections]
    names = [REFERENCES, *pack_names, GAME]
    total_bytes = sum(release["files"][name]["bytes"] for name in names)
    # Only verified archives contribute to the completed byte count.
    completed_bytes = 0
    archives = {}
    for index, name in enumerate(names, 1):
        archive_bytes = release["files"][name]["bytes"]

        def report(action, bytes_read):
            if progress:
                progress(action, index, len(names), bytes_read, archive_bytes,
                         completed_bytes + bytes_read, total_bytes)

        # Repair can use a different directory after the player moves the downloads.
        selected = game if name == GAME else game.with_name(name)
        archives[name] = obtain(name, selected, cache, release, progress=report)
        report("checked", archive_bytes)
        completed_bytes += archive_bytes
    if progress:
        progress("unpacking", len(names), len(names), 0, 0, completed_bytes, total_bytes)
    with ZipFile(archives[REFERENCES]) as archive:
        if tuple(archive.namelist()) != MEMBERS:
            raise ValueError(f"{REFERENCES} has an unexpected layout.")
        for name in MEMBERS:
            with archive.open(name) as source, (work / name).open("wb") as output:
                copyfileobj(source, output, length=1024 * 1024)
    packs = {name: str(archives[name]) for name in pack_names}
    (work / "lite-packs.json").write_text(json.dumps(packs), encoding="utf-8")
    (work / "lite-media.ini").write_text(f"[media]\ngame={archives[GAME]}\n", encoding="utf-16")


def main(argv=None, *, progress=None):
    """Run media staging with an optional progress callback from Setup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game", type=Path)
    parser.add_argument("wow", type=Path)
    parser.add_argument("cache", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("--pack-mask", type=int, choices=(-1, *PACK_MASKS), default=-1)
    parser.add_argument("--include-installed", action="store_true")
    args = parser.parse_args(argv)
    try:
        release = json.loads((args.work / "lite-media.json").read_bytes())
        stage(args.game, args.wow, args.cache, args.work, release,
              pack_mask=None if args.pack_mask == -1 else args.pack_mask,
              include_installed=args.include_installed, progress=progress)
    except (OSError, ValueError, BadZipFile) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
