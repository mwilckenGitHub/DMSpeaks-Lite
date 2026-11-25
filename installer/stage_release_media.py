"""Verify and prepare the content downloads for Windows Lite Setup."""

import argparse
from hashlib import file_digest, sha256
import json
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


def playback_zip(collection):
    return f"DMSpeaks-Lite-{collection}-Audio.zip"


def matches(path, record):
    if not path.is_file() or path.is_symlink() or path.stat().st_size != record["bytes"]:
        return False
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest() == record["sha256"]


def download(url, target, record):
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with NamedTemporaryFile(dir=target.parent, delete=False) as output:
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
            if size != record["bytes"] or checksum.hexdigest() != record["sha256"]:
                raise ValueError(f"{target.name} does not match this release.")
        pending.replace(target)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def obtain(name, selected, cache, release):
    record = release["files"][name]
    cached = cache / name
    if selected.exists():
        if not matches(selected, record):
            raise ValueError(f"{selected.name} does not match this release.")
        return selected
    if matches(cached, record):
        return cached
    base = release.get("download_base")
    if not base:
        raise ValueError(f"Put {name} beside Setup.")
    if not re.fullmatch(r"https://github\.com/mwilckenGitHub/DMSpeaks-Lite/releases/download/[A-Za-z0-9._-]+", base):
        raise ValueError("The Lite release download address is invalid.")
    print(f"Downloading {name}...", flush=True)
    download(base + "/" + quote(name), cached, record)
    return cached


def stage(game, wow, cache, work, release):
    addons = wow / "Interface" / "AddOns"
    collections = [name for name in COLLECTIONS
                   if (addons / f"SpokenQuestsAudio{name}" / f"SpokenQuestsAudio{name}.toc").is_file()]
    if not collections:
        raise ValueError("Install an English Spoken voice pack in this WoW folder, then run Setup again.")
    # Repair can use a different directory after the player moves the downloads.
    references = obtain(REFERENCES, game.with_name(REFERENCES), cache, release)
    packs = {}
    for collection in collections:
        name = playback_zip(collection)
        packs[name] = str(obtain(name, game.with_name(name), cache, release))
    game = obtain(GAME, game, cache, release)
    with ZipFile(references) as archive:
        if tuple(archive.namelist()) != MEMBERS:
            raise ValueError(f"{REFERENCES} has an unexpected layout.")
        for name in MEMBERS:
            with archive.open(name) as source, (work / name).open("wb") as output:
                copyfileobj(source, output, length=1024 * 1024)
    (work / "lite-packs.json").write_text(json.dumps(packs), encoding="utf-8")
    (work / "lite-media.ini").write_text(f"[media]\ngame={game}\n", encoding="utf-16")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game", type=Path)
    parser.add_argument("wow", type=Path)
    parser.add_argument("cache", type=Path)
    parser.add_argument("work", type=Path)
    args = parser.parse_args()
    try:
        release = json.loads((args.work / "lite-media.json").read_bytes())
        stage(args.game, args.wow, args.cache, args.work, release)
    except (OSError, ValueError, BadZipFile) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
