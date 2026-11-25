"""Refresh or repair application-owned source from the Windows full installer."""

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import sys
from zipfile import BadZipFile, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply_beta_update as beta
from open_lite import installed_build_id, probe, read_snapshot


def repair_entries(package, archive, manifest):
    """Verify the complete source image before staging changed files."""
    files = manifest["files"]
    members = archive.namelist()
    if len(members) != len(files) + 1 or set(members) != {"update.json", *files}:
        raise ValueError("The full Setup program archive is incomplete.")
    source = package / "source"
    fingerprint = sha256()
    checked, missing, entries = {source}, [], []
    for name in sorted(files):
        relative = PurePosixPath(name)
        if (relative.is_absolute() or relative.as_posix() != name or ".." in relative.parts
                or "\\" in name or ":" in name or not relative.name):
            raise ValueError("Setup contains an invalid program filename.")
        expected = files[name][1]
        actual = sha256(archive.read(name)).digest()
        if actual.hex() != expected:
            raise ValueError(f"Setup contains a damaged program file: {name}")
        if ((len(relative.parts) == 1 and relative.suffix == ".py")
                or relative.parts[0] in {"formats", "lite_addon", "lite_web", "third_party"}):
            if "__pycache__" not in relative.parts and relative.suffix != ".pyc":
                fingerprint.update(name.encode("utf-8") + b"\0" + actual)
        if beta.install_addon.installed_digest(source, name, checked, missing) != expected:
            entries.append((archive.getinfo(name), relative))
    for name in ("Prepared-dialogue-plans.sha256", "Prepared-paced-deliveries.json"):
        pin = package / name
        if pin.is_file():
            fingerprint.update(name.encode("ascii") + b"\0" + sha256(pin.read_bytes()).digest())
    if fingerprint.hexdigest() != manifest["after"]:
        raise ValueError("The full Setup program archive does not match this edition.")
    return entries


def running_companion(game):
    # The local identity algorithm is part of the launcher's HTTP protocol.
    # Read it without importing any of the installed modules being repaired.
    def client_id(folder):
        return sha256(os.path.normcase(str(folder.resolve())).encode("utf-8")).hexdigest()

    data = Path(os.environ["LOCALAPPDATA"]) / "DMSpeaksLiteBeta"
    addons = game / "Interface" / "AddOns"
    for name in ("DMSpeaksLiteStatus/Snapshot.lua", "DMSpeaksLite/Status.lua"):
        port, _ = read_snapshot(addons / name)
        if port and 0 < port <= 65535 and probe(port, game, data, client_id):
            return True
    return False


def refresh(package, game, path):
    source = package / "source"
    work = package / ".beta-update"
    lock = package / ".beta-update.lock"
    if any(beta.install_addon.is_link(item) for item in (source, work, lock)):
        raise ValueError("The installed Lite program path is linked.")
    with beta.install_addon.placement_lock(lock), ZipFile(path) as archive:
        manifest = json.loads(archive.read("update.json"))
        known = {manifest["before"], manifest["after"], *manifest.get("compatible_builds", [])}
        if manifest.get("complete_source") is True:
            beta.install_addon.clean_or_recover(source, work)
            entries = repair_entries(package, archive, manifest)
        else:
            entries = beta.apply_runtime(package, archive, manifest)
        before = installed_build_id(package)
        if before not in known:
            if not entries or manifest.get("complete_source") is not True:
                raise ValueError("This setup could not identify the installed Lite build.")
            if running_companion(game):
                raise ValueError("Close the running DMSpeaks Lite companion and retry Setup to repair its program files.")
        elif entries and running_companion(game):
            beta.retire_listener(package, game, manifest["after"])
        if entries:
            source.mkdir(exist_ok=True)
            beta.replace_runtime(package, archive, entries, manifest["after"])


if __name__ == "__main__":
    try:
        package = Path(sys.executable).resolve().parents[2]
        game, archive = (Path(item).resolve() for item in sys.argv[1:])
        refresh(package, game, archive)
    except (OSError, sqlite3.Error, ValueError, BadZipFile, KeyError) as error:
        print(str(error), flush=True)
        raise SystemExit(1)
