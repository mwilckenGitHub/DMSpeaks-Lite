"""Maintain the shared Windows shell after resolving the surviving WoW clients."""
from argparse import ArgumentParser
from hashlib import file_digest, sha256
import gzip
import json
import os
from pathlib import Path
import stat
import sys

import prune_runtimes as runtime


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def shell_links(product):
    name = "DMSpeaks Lite - WOW.lnk"
    if runtime.installed_games(product)[0]:
        programs, desktop = runtime.known_folder(2), runtime.known_folder(16)
    else:
        programs = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
        desktop = Path(os.environ["USERPROFILE"]) / "Desktop"
    return (product / name, programs / name, desktop / name)


def is_game_link(link, builds, game):
    if not runtime.ordinary(link):
        return False
    target, raw_args = runtime.shortcut(link)
    if not target.is_relative_to(builds):
        return False
    parts = target.relative_to(builds).parts
    if len(parts) != 4 or tuple(s.lower() for s in parts[1:]) != ("runtime", "python", "pythonw.exe"):
        return False
    args = runtime.windows_args(raw_args)
    return (len(args) == 4 and args[:2] == ["-I", "-B"]
            and runtime.key(Path(args[2])) == runtime.key(builds.parent / "Maintenance/open_lite.py")
            and runtime.key(Path(args[3])) == runtime.key(game))


def stop_check(build):
    """Only pass an unavailable stop helper when no process uses the target runtime."""
    if any(path.is_relative_to(build) for path in runtime.process_images()):
        raise OSError("Close this WoW installation's Lite desktop app before retrying.")
    print("The desktop runtime has no running process.", flush=True)


SHELL_FILES = ("open_lite.py", "install_addon.py", "addon-files.txt.gz", "stock-files.json.gz", "DMSpeaks-Orc.ico")


def file_pin(path):
    # Match the [length, SHA-256] schema used by the shipped runtime inventory.
    return [path.stat().st_size, digest(path)]


def reconcile_orphan(build, inputs):
    """Admit the empty, unregistered build left by an interrupted or older uninstall."""
    builds = runtime.trusted_path(Path(os.environ["LOCALAPPDATA"]) / "DMSpeaksLite/Builds")
    if build.parent != builds:
        raise ValueError("Unexpected runtime location.")
    packed = Path(runtime.__file__).with_name("runtime-files.json.gz").read_bytes()
    if sha256(packed).hexdigest() != runtime.INVENTORY_SHA256:
        raise ValueError("Runtime ownership inventory verification failed.")
    if build.name not in json.loads(gzip.decompress(packed))["builds"]:
        raise ValueError("Unknown runtime build.")

    with runtime.serialize(builds):
        product = builds.parent
        maintenance = runtime.trusted_path(product / "Maintenance")
        real, registered = runtime.installed_games(product)
        links = runtime.shortcut_clients(builds, real)
        claims = [runtime.references(runtime.trusted_path(folder))
                  for folder in (product / "RuntimeClients", maintenance / "RuntimeClients")]
        for client in registered:
            if client not in links and not any(client in records for records in claims):
                raise ValueError("A registered game's runtime cannot be identified.")
        if any(build.name in ids for ids in links.values()) or any(build.name in records.values() for records in claims):
            raise ValueError("A game still owns this runtime. Use its maintenance options.")
        stop_check(build)
        if not runtime.ordinary(build) or not stat.S_ISDIR(build.stat().st_mode):
            raise ValueError("The old build is not an ordinary directory.")
        marker = build / runtime.UPDATE_LOCK
        for item in build.iterdir():
            if item != marker or not runtime.ordinary(item) or not stat.S_ISREG(item.stat().st_mode):
                raise ValueError("The old runtime contains additional files. Use uninstall recovery.")
        # The fresh setup will write these same five files. Protect customized shell
        # files in the unregistered remainder before making any changes on disk.
        for name in SHELL_FILES:
            path = maintenance / name
            if runtime.ordinary(path) and (not stat.S_ISREG(path.stat().st_mode)
                                         or file_pin(path) != file_pin(inputs / name)):
                raise ValueError("The old maintenance folder contains a changed program file.")
        if runtime.ordinary(maintenance / "DMSpeaks-Maintenance.exe") and not registered:
            raise ValueError("The old maintenance executable cannot be verified.")
        # prune hashes and deletes through the same handle and only removes empty
        # directories. A locked or changed sentinel must not authorize replacement.
        allowed = [[0, sha256(b"").hexdigest()]]
        runtime.prune(build, {marker.name: allowed})
        if runtime.ordinary(build):
            raise ValueError("The old runtime could not be cleared safely. Use uninstall recovery.")


def remove_last(build, game, inputs):
    product = build.parent.parent
    maintenance = runtime.trusted_path(product / "Maintenance")
    # Use the same path+size+hash removal as runtime cleanup. Unknown files can coexist.
    expected = {name: [file_pin(inputs / name)] for name in SHELL_FILES}
    expected["DMSpeaks.ico"] = [file_pin(inputs / "legacy.ico")]
    for link in shell_links(product):
        runtime.trusted_path(link.parent)
        if is_game_link(link, build.parent, game):
            link.unlink()
    runtime.prune(maintenance, expected)
    # The registered uninstaller is removed last. Running from the NSIS temporary
    # directory also permits read-only recovery when the original binary is absent.
    target = maintenance / "DMSpeaks-Maintenance.exe"
    if runtime.ordinary(target):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, runtime.REGISTRATION + r"\DMSpeaksLite",
                            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            checksum = winreg.QueryValueEx(key, "MaintenanceSHA256")[0]
        if digest(target) == checksum:
            target.unlink()


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("mode", choices=("stop-check", "remove-marker", "remove-last", "record", "reconcile-orphan"))
    parser.add_argument("build", type=Path)
    parser.add_argument("game", type=Path)
    parser.add_argument("inputs", type=Path)
    args = parser.parse_args()
    try:
        build = runtime.trusted_path(args.build)
        game = runtime.trusted_path(args.game)
        installed = build.parent.parent / "Maintenance/DMSpeaks-Maintenance.exe"
        if args.mode == "stop-check":
            stop_check(build)
        elif args.mode == "reconcile-orphan":
            reconcile_orphan(build, args.inputs)
        elif args.mode == "remove-marker":
            import winreg
            marker = runtime.trusted_path(game / "Interface/.dmspeaks-setup-version.txt")
            if runtime.ordinary(marker):
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, runtime.REGISTRATION + r"\DMSpeaksLite",
                                    access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                    revision = winreg.QueryValueEx(key, "CleanupRevision")[0]
                if marker.stat().st_size <= 68 and marker.read_bytes().strip() == revision.encode("ascii"):
                    marker.unlink()
        elif args.mode == "record":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, runtime.REGISTRATION + r"\DMSpeaksLite",
                                access=winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                winreg.SetValueEx(key, "MaintenanceSHA256", 0, winreg.REG_SZ, digest(installed))
        else:
            remove_last(build, game, args.inputs)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
