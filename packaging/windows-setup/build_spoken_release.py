"""Repack a verified Lite Windows build with separate release media."""

import argparse
import gzip
import json
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build as native
from spoken_game_media import PACK_ROOTS, placement_script, split_game


ROOT = Path(__file__).resolve().parents[2]
MEDIA = "DMSpeaks-Lite-Name-Data.zip"
MEDIA_MEMBERS = ("embedded-catalog.zip", "prefix-data.zip")
MEDIA_PROGRESS_END = 20  # Leave five points for program extraction before voice placement.

# The published Beta 12.3 and the first profile-action build share this runtime directory.
COMPATIBLE_DESKTOP_BUILDS = {
    "76d402a54a15946b6bfc0f39ace41f1474fd9ecde1e9f6a9c3bc251c98880a7d",
    "a2f530f564611eab49ada40afe28d9a1770d892b661d876c5bea809dbfdd390f",
}


def record(path):
    return {"sha256": native.digest(path), "bytes": path.stat().st_size}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def update_program(payload, support, work, previous):
    """Refresh the packaged UI and regenerate its verification records."""
    source = payload / "source"
    source_root = ROOT / "source"
    changed = {}
    paths = [path.relative_to(source) for path in source.glob("lite_web/*.png")]
    paths.extend(path.relative_to(source) for path in source.glob("lite_addon/Icons/*.tga"))
    paths.extend(Path(name) for name in (
        "lite_addon/Settings.lua",
        "lite_game_ingress.py",
        "lite_web/desktop-help.js",
        "lite_web/index.html",
        "lite_web/lite.css",
        "lite_web/lite.js",
        "lite_web/locale.js",
        "lite_web/menu-help-en.js",
    ))
    for relative in sorted(paths):
        path = source / relative
        path.write_bytes((source_root / relative).read_bytes())
        changed["source/" + relative.as_posix()] = path
    ffmpeg = payload / "runtime/bin/ffmpeg.exe"
    icon = work / "waveform.ico"
    placeholder = work / "placeholder.jpg"
    probes = (
        (source_root / "lite_web/dmspeaks-mark.png", icon, ("-c:v", "bmp", "-pix_fmt", "bgra")),
        (source_root / "lite_web/unknown.png", placeholder, ("-c:v", "mjpeg")),
    )
    for graphic, target, options in probes:
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", graphic,
                        "-frames:v", "1", *options, target], check=True)
    for path in sorted(source.glob("lite_web/blizzard-avatars/*.jpg")):
        path.write_bytes(placeholder.read_bytes())
        changed["source/" + path.relative_to(source).as_posix()] = path

    refresh = work / "runtime-update.zip"
    with ZipFile(support / "runtime-update.zip") as archive:
        manifest = json.loads(archive.read("update.json"))
    fingerprint = sha256()
    for name in sorted(manifest["files"]):
        path = source / name
        digest = native.digest(path)
        if "source/" + name not in changed and digest != manifest["files"][name][1]:
            raise ValueError(f"Registered runtime file differs: {name}")
        manifest["files"][name][1] = digest
        relative = Path(name)
        if ((len(relative.parts) == 1 and relative.suffix == ".py")
                or relative.parts[0] in {"formats", "lite_addon", "lite_web", "third_party"}):
            if "__pycache__" not in relative.parts and relative.suffix != ".pyc":
                fingerprint.update(name.encode("utf-8") + b"\0" + bytes.fromhex(digest))
    for name in ("Prepared-dialogue-plans.sha256", "Prepared-paced-deliveries.json"):
        if (payload / name).is_file():
            fingerprint.update(name.encode("ascii") + b"\0" + bytes.fromhex(native.digest(payload / name)))
    manifest["compatible_builds"] = sorted(
        set(manifest["compatible_builds"]) | COMPATIBLE_DESKTOP_BUILDS | {manifest["after"]}
    )
    manifest["after"] = fingerprint.hexdigest()
    with ZipFile(refresh, "x", compression=ZIP_DEFLATED) as archive:
        archive.writestr("update.json", json.dumps(manifest, sort_keys=True))
        for name in sorted(manifest["files"]):
            archive.write(source / name, name)

    cleanup = support / "cleanup"
    old_inventory = cleanup / "runtime-files.json.gz"
    inventory = json.loads(gzip.decompress(old_inventory.read_bytes()))
    owned = inventory["builds"][previous["registered_build"]]
    for name, path in changed.items():
        if name not in owned:
            owned[name] = [inventory["common"][name]]
        pin = [path.stat().st_size, native.digest(path)]
        if pin not in owned[name]:
            owned[name].append(pin)
    ownership = work / old_inventory.name
    ownership.write_bytes(gzip.compress(json.dumps(inventory, sort_keys=True).encode(), mtime=0))
    pruner = work / "prune_runtimes.py"
    body = (cleanup / pruner.name).read_bytes()
    old = native.digest(old_inventory).encode("ascii")
    if body.count(old) != 1:
        raise ValueError("The verified runtime pruner does not bind its ownership manifest.")
    pruner.write_bytes(body.replace(old, native.digest(ownership).encode("ascii")))
    return refresh, icon, ownership, pruner, manifest["after"]


def page(target, files):
    target.write_text("\n\n".join(path.read_text(encoding="utf-8-sig").rstrip() for path in files)
                      + "\n", encoding="utf-16", newline="\r\n")
    return target


def update_game_ownership(support, work, placement, settings):
    original = support / "stock-files.json.gz"
    inventory = json.loads(gzip.decompress(original.read_bytes()))
    settings_hashes = inventory["current"]["DMSpeaksLite/Settings.lua"]
    settings_hash = native.digest(settings)
    if settings_hash not in settings_hashes:
        settings_hashes.append(settings_hash)
    target = work / original.name
    target.write_bytes(gzip.compress(json.dumps(inventory, sort_keys=True).encode(), mtime=0))
    text = placement.read_text(encoding="utf-8")
    pin = native.digest(original)
    if text.count(pin) != 1:
        raise ValueError("The verified game placement does not bind its ownership manifest.")
    placement.write_text(text.replace(pin, native.digest(target)), encoding="utf-8", newline="\n")
    return target


def build(args):
    receipt = args.previous / "verified.json"
    native.require(receipt, args.previous_sha256)
    previous = native.read(receipt)
    files = args.previous / "Install files"
    for name in (native.BASE_SETUP, native.GAME):
        native.require(files / name, previous["files"][name]["sha256"])
    native.require(files / native.GAME, previous["transport"]["compact_archive_sha256"])
    compiler = args.tools / "nsis-3.09/Bin/makensis.exe"
    seven = args.tools / "7zip-22.01/7z.exe"
    lock = native.read(native.HERE / "native-lock.json")["tools"]
    for path in (compiler, seven, seven.with_name("7z.dll")):
        native.require(path, lock[path.relative_to(args.tools).as_posix()])
    args.output.mkdir(parents=True, exist_ok=False)
    product = args.output / "Install files"
    product.mkdir()
    # MakeNSIS 3.09 uses legacy Win32 path limits for its File instruction.
    with TemporaryDirectory(prefix="dmsl-") as temp:
        work = Path(temp)
        base = work / "base"
        native.extract(seven, files / native.BASE_SETUP, base)
        support = base / "$PLUGINSDIR"
        payload = work / "program"
        shutil.copytree(base / "$2", payload)
        native.require(support / MEDIA_MEMBERS[0], previous["transport"]["embedded_catalog_zip_sha256"])
        prefix_hash = native.read(native.HISTORY / "shipping-readback.json")["prefix_sha256"]
        native.require(support / MEDIA_MEMBERS[1], prefix_hash)
        refresh, icon, ownership, pruner, build_id = update_program(payload, support, work, previous)
        with ZipFile(product / MEDIA, "x", compression=ZIP_STORED) as archive:
            for name in MEDIA_MEMBERS:
                archive.write(support / name, name)
        settings_file = ROOT / "source/lite_addon/Settings.lua"
        split_game(files / native.GAME, product, settings_file)
        info = {"files": {name: record(product / name) for name in (native.GAME, MEDIA, *PACK_ROOTS)}}
        placement = placement_script(support / "install_addon.py", work / "install_addon.py",
                                     {name: info["files"][name]["sha256"] for name in (native.GAME, *PACK_ROOTS)})
        stock = update_game_ownership(support, work, placement, settings_file)
        if args.release_tag:
            info["download_base"] = ("https://github.com/mwilckenGitHub/DMSpeaks-Lite/releases/download/"
                                     + args.release_tag)
        media = work / "lite-media.json"
        write_json(media, info)
        setup_sources = (*native.SETUP_SOURCES, "VoicePackPage.nsh")
        staging = ROOT / "installer/stage_release_media.py"
        progress = ROOT / "installer/setup_media_progress.py"
        setup_inputs = (media, ownership, pruner, staging, progress,
                        *(ROOT / "installer" / name for name in setup_sources))
        setup_id = sha256("".join(native.digest(path) for path in setup_inputs).encode("ascii")).hexdigest()
        recipe = work / "installer"
        recipe.mkdir()
        for name in setup_sources:
            body = (ROOT / "installer" / name).read_bytes()
            (recipe / name).write_bytes(body.replace(b"${__FILEDIR__}/", b"${__FILEDIR__}\\"))
        for name, packaged_name in (("maintenance-core.zip", "maintenance-core.zip"),
                                    ("maintenance-python._pth", "python313._pth"),
                                    ("shell_maintenance.py", "shell_maintenance.py")):
            shutil.copyfile(support / "cleanup" / packaged_name, recipe / name)
        includes = work / "payload.nsh"
        native.payload_commands(payload, includes, progress_start=MEDIA_PROGRESS_END)
        python = work / "python.nsh"
        runtime = payload / "runtime/python"
        members = sorted(p for p in runtime.iterdir() if p.is_file() and p.name != "pythonw.exe"
                         and p.suffix.lower() in {".dll", ".pyd", ".zip", ".exe"})
        python.write_text("".join(f'File "{path}"\n' for path in members), encoding="utf-8")
        notices = [args.previous / "product" / name for name in previous["install_notices"]]
        for path in notices:
            native.require(path, previous["install_notices"][path.name])
        agreement = ROOT / "EULA.md"
        native.require(agreement, previous["end_user_agreement"]["EULA.md"])
        settings = dict(
            SETUP_ID=setup_id, ICON=icon, OUTPUT=product / native.BASE_SETUP,
            BUILD=previous["registered_build"], PAYLOAD=payload, PAYLOAD_COMMANDS=includes,
            MAINTENANCE_PYTHON_FILES=python, RUNTIME_PRUNER=pruner, RUNTIME_OWNERS=ownership,
            END_USER_AGREEMENT=page(recipe / "agreement.txt", [agreement]),
            LICENSE_NOTICES=page(recipe / "notices.txt", notices),
            PLACEMENT=placement, OWNERS=support / "addon-files.txt.gz",
            STOCK_OWNERS=stock, COMPATIBILITY_ICON=icon, LEGACY_ICON=icon,
            REFRESH=support / "refresh_runtime.py", OPEN_LITE_SCRIPT=support / "open_lite.py",
            UPDATE_SCRIPT=support / "apply_beta_update.py", RUNTIME_DELTA=refresh, EXTERNAL_MEDIA=media,
            STAGE_RELEASE_MEDIA=staging, STAGE_MEDIA_PROGRESS=progress,
            RELEASE_MEDIA_PROGRESS=MEDIA_PROGRESS_END,
            PREFIX_INSTALL_SCRIPT=ROOT / "installer/install_prefix_media.py",
            PREFIX_ARCHIVE_HASH=prefix_hash, PREFIX_MANIFEST_HASH=native.PREFIX_MANIFEST,
        )
        print("Compiling Lite Setup with separate release media.", flush=True)
        native.compile_script(compiler, recipe / "Setup.nsi", settings, args.output / "setup.log")
        extracted = work / "extracted"
        native.extract(seven, product / native.BASE_SETUP, extracted)
        native.compare("desktop payload", native.inventory(extracted / "$2"), native.inventory(payload))
        maintenance = work / "maintenance"
        native.extract(seven, extracted / native.WIZARD, maintenance)
        for shell in (extracted, maintenance):
            found = shell / "$PLUGINSDIR"
            native.require(found / "stage_release_media.py", native.digest(settings["STAGE_RELEASE_MEDIA"]))
            native.require(found / "setup_media_progress.py", native.digest(progress))
            native.require(found / "lite-media.json", native.digest(media))
            if any((found / name).exists() for name in (*MEDIA_MEMBERS, native.GAME, MEDIA)):
                raise ValueError("Release media was embedded in the executable.")
        result = {"files": {p.name: record(p) for p in sorted(product.iterdir())},
                  "registered_build": previous["registered_build"], "desktop_build": build_id,
                  "cleanup_revision": setup_id, "media": info, "prior_release": args.previous_sha256}
        if any(r["bytes"] >= 2**31 for r in result["files"].values()):
            raise ValueError("A release asset exceeds GitHub's per-file size limit.")
        write_json(args.output / "verified.json", result)
        print(product, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("previous", "tools", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--previous-sha256", required=True)
    parser.add_argument("--release-tag", help="Enable fetching the ZIPs from this public GitHub release")
    args = parser.parse_args()
    if args.release_tag and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.release_tag):
        parser.error("Use a simple GitHub release tag.")
    build(args)
