"""Rebuild the registered Beta 12 base Setup and Recovery with native Windows tools."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
HISTORY = HERE / "historical"
BASE_SETUP = "DMSpeaks Lite - WOW Setup.exe"
RECOVERY = "DMSpeaks Uninstall Recovery.exe"
GAME = "DMSpeaks-WoW.zip"
WIZARD = Path("$LOCALAPPDATA/DMSpeaksLite/Maintenance/$OUTDIR/DMSpeaks-Maintenance.exe")
SETUP_SOURCES = ("Setup.nsi", "UninstallRecovery.nsi", "DMSpeaksLanguages.nsh", "RuntimeCleanup.nsh")
MAINTENANCE_SUPPORT = ("shell_maintenance.py", "maintenance-core.zip", "maintenance-python._pth")
PREFIX_MANIFEST = "899953333aed9161083437137058f744fe736d4243a5666d6a5ee70a9fe4808c"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(path, expected):
    actual = digest(path)
    if actual != expected:
        raise ValueError(f"{path} expected {expected}, got {actual}")
    return actual


def inventory(folder):
    return {path.relative_to(folder).as_posix(): digest(path)
            for path in folder.rglob("*") if path.is_file()}


def compare(label, actual, expected):
    keys = actual.keys() | expected.keys()
    differences = sorted(key for key in keys if actual.get(key) != expected.get(key))
    if differences:
        raise ValueError(f"{label}: {len(differences)} differences, including {differences[:15]}")


def extract(seven, archive, dest, *members):
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run([seven, "x", "-y", "-bso0", "-bsp0", "-o" + str(dest), archive, *members], check=True)


def linktree(source, target):
    # Keep NSIS compile-time paths free of $PLUGINSDIR / $2 pseudo-folder names.
    def copy(src, dst):
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
        return dst
    shutil.copytree(source, target, copy_function=copy)


def payload_commands(payload, dest, *, progress_start=0):
    """Emit NSIS extraction steps in the portion of the bar reserved for program files."""
    directories = []
    total = 0
    for folder, subs, names in os.walk(payload):
        subs.sort()
        files = [(Path(folder, name).relative_to(payload), Path(folder, name).stat().st_size)
                 for name in sorted(names)]
        total += sum(size for _, size in files)
        directories.append((Path(folder).relative_to(payload), files))
    written = 0
    last = progress_start
    with dest.open("w", encoding="utf-8") as commands:
        for folder, files in directories:
            suffix = "" if folder == Path(".") else "\\" + str(folder).replace("$", "$$")
            print(f'SetOutPath "$2{suffix}"', "IfErrors unpack_error", sep="\n", file=commands)
            for relative, size in files:
                print(f'File "${{PAYLOAD}}\\{relative}"', file=commands)
                written += size
                percent = progress_start + (25 - progress_start) * written // max(1, total)
                if percent > last:
                    print("IfErrors unpack_error", f'!insertmacro ShowOverall {percent} "$(InstallingProgramPhase)"',
                          sep="\n", file=commands)
                    last = percent


def compile_script(compiler, script, defines, log):
    args = [compiler, "/NOCD", "/INPUTCHARSET", "UTF8", "/V3"]
    args.extend(f"/D{name}={value}" for name, value in defines.items())
    args.append(script)
    with log.open("wb") as stream:
        result = subprocess.run(args, env={**os.environ, "NSISDIR": str(compiler.parents[1])},
                                cwd=script.parent, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        text = log.read_bytes()
        encoding = "utf-16" if text.startswith(b"\xff\xfe") else "utf-8"
        raise RuntimeError(log.name + "\n" + text.decode(encoding, "replace")[-2400:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Fresh directory for verified artifacts and receipt")
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--kit", type=Path, help="Optionally check the original full kit, or use it to reconstruct absent extracted inputs")
    parser.add_argument("--notices", type=Path, default=HERE / "notices",
                        help="Directory containing the release's LICENSE, THIRD_PARTY.md and THIRD-PARTY-RUNTIMES.md")
    opts = parser.parse_args()
    if os.name != "nt":
        parser.error("This recipe requires Windows.")
    output, inputs, tools = (p.resolve() for p in (opts.output, opts.inputs, opts.tools))
    kit = opts.kit.resolve() if opts.kit else None
    output.mkdir(parents=True, exist_ok=False)
    compiler = tools / "nsis-3.09/Bin/makensis.exe"
    seven = tools / "7zip-22.01/7z.exe"
    baseline = read(HISTORY / "build-receipt.json")
    locks = read(HERE / "native-lock.json")
    for section, root in (("tools", tools), ("inputs", inputs), ("history", HISTORY)):
        for rel, expected in locks[section].items():
            require(root / rel, expected)
    if kit is not None:
        for filename in (BASE_SETUP, RECOVERY, GAME):
            require(kit / filename, baseline["kit_files"][filename]["sha256"])
    frozen = inputs / "frozen-installer"
    source = HERE.parents[1] / "installer"
    original = inputs / "extracted"
    with TemporaryDirectory(prefix="native-work-", dir=output) as tmp:
        temp = Path(tmp)
        if not original.is_dir():
            if kit is None:
                raise FileNotFoundError(f"Missing {original}. Supply --kit to reconstruct it.")
            original = temp / "original"
            extract(seven, kit / BASE_SETUP, original)
        desktop_files = read(HISTORY / "desktop-package.json")
        compare("baseline desktop", inventory(original / "$2"), desktop_files)
        compare("baseline installer support", inventory(original / "$PLUGINSDIR"), locks["original_support"])
        golden_maintenance = original / WIZARD
        require(golden_maintenance, baseline["maintenance_sha256"])
        extract(seven, golden_maintenance, temp / "original-wizard")
        require(inputs / "legacy.ico", digest(temp / "original-wizard/$PLUGINSDIR/legacy.ico"))
        payload = temp / "payload"
        plug = temp / "support"
        linktree(original / "$2", payload)
        linktree(original / "$PLUGINSDIR", plug)
        recipe = temp / "installer"
        recipe.mkdir()
        for name in SETUP_SOURCES:
            if name not in ("Setup.nsi", "DMSpeaksLanguages.nsh"):
                require(source / name, digest(frozen / name))
            # Windows MakeNSIS 3.09 treats / literally in a mixed include path.
            # Translate only the compile-time directory separator in the original recipe.
            content = (source / name).read_bytes().replace(b"${__FILEDIR__}/", b"${__FILEDIR__}\\")
            (recipe / name).write_bytes(content)
        for name in MAINTENANCE_SUPPORT:
            shutil.copy2(frozen / name, recipe / name)
        for name in ("install_addon.py", "open_lite.py", "stock-files.json.gz"):
            require(plug / name, digest(frozen / name))
        prefix_pin = read(HISTORY / "shipping-readback.json")["prefix_sha256"]
        require(plug / "prefix-data.zip", prefix_pin)
        payload_include = temp / "payload.nsh"
        payload_commands(payload, payload_include)
        runner = sorted(p for p in (payload / "runtime/python").iterdir() if p.is_file() and
                        p.name != "pythonw.exe" and p.suffix.lower() in {".dll", ".pyd", ".zip", ".exe"})
        python_include = temp / "python.nsh"
        python_include.write_text("".join(f'File "{p}"\n' for p in runner), encoding="utf-8")
        common = dict(ICON=frozen / "DMSpeaks-Waveform.ico", SETUP_ID=baseline["setup_id"])
        notices = ("LICENSE", "THIRD_PARTY.md", "THIRD-PARTY-RUNTIMES.md")
        notice_text = "\n\n".join((opts.notices / name).read_text(encoding="utf-8-sig").rstrip() for name in notices)
        notice_file = recipe / "notices.txt"
        notice_file.write_text(notice_text + "\n", encoding="utf-16", newline="\r\n")
        notice_hashes = {name: digest(opts.notices / name) for name in notices}
        agreement = HERE.parents[1] / "EULA.md"
        agreement_file = recipe / "end-user-agreement.txt"
        agreement_file.write_text(agreement.read_text(encoding="utf-8-sig"), encoding="utf-16", newline="\r\n")
        settings = dict(**common, END_USER_AGREEMENT=agreement_file, LICENSE_NOTICES=notice_file,
                        OUTPUT=output / BASE_SETUP, BUILD="5996a8fb357ae6741b77", PAYLOAD=payload,
                        PAYLOAD_COMMANDS=payload_include, MAINTENANCE_PYTHON_FILES=python_include,
                        RUNTIME_PRUNER=frozen / "prune_runtimes.py", RUNTIME_OWNERS=frozen / "runtime-files.json.gz",
                        STOCK_OWNERS=plug / "stock-files.json.gz", PLACEMENT=plug / "install_addon.py",
                        DELTA=plug / "addon-update.zip", OWNERS=plug / "addon-files.txt.gz",
                        COMPATIBILITY_ICON=plug / "DMSpeaks-Orc.ico", LEGACY_ICON=inputs / "legacy.ico",
                        OPEN_LITE_SCRIPT=plug / "open_lite.py", RUNTIME_DELTA=plug / "runtime-update.zip",
                        REFRESH=plug / "refresh_runtime.py", UPDATE_SCRIPT=plug / "apply_beta_update.py",
                        PREFIX_ARCHIVE=plug / "prefix-data.zip", PREFIX_ARCHIVE_HASH=prefix_pin,
                        PREFIX_MANIFEST_HASH=PREFIX_MANIFEST, PREFIX_INSTALL_SCRIPT=plug / "install_prefix_media.py")
        print("Compiling x64 Setup on Windows", flush=True)
        compile_script(compiler, recipe / "Setup.nsi", settings, output / "setup.log")
        checked = temp / "rebuilt"
        extract(seven, output / BASE_SETUP, checked)
        maintenance = checked / WIZARD
        rebuilt_wizard = digest(maintenance)
        # Native build timestamps and installer bytecode location can change the PE shells.
        # Every extracted file except the outer generated maintenance PE must be identical.
        expected_files = inventory(original)
        actual_files = inventory(checked)
        for files in (expected_files, actual_files):
            del files[WIZARD.as_posix()]
        compare("entire installed payload", actual_files, expected_files)
        extract(seven, maintenance, temp / "rebuilt-wizard")
        wizard_files = inventory(temp / "rebuilt-wizard")
        compare("embedded maintenance", wizard_files, inventory(temp / "original-wizard"))
        print("Compiling x64 Recovery from the new Setup's maintenance executable", flush=True)
        # 7-Zip gives a generated uninstaller the extraction time. Preserve the
        # original Recovery member's UTC timestamp (2026-11-24T09:54:46Z).
        os.utime(maintenance, (1795514086, 1795514086))
        compile_script(compiler, recipe / "UninstallRecovery.nsi",
                       dict(**common, OUTPUT=output / RECOVERY, SHIPPING_MAINTENANCE=maintenance),
                       output / "recovery.log")
        extract(seven, output / RECOVERY, temp / "recovery")
        compare("Recovery", inventory(temp / "recovery"), {"$PLUGINSDIR/maintenance.exe": rebuilt_wizard})
        report = dict(base_build=settings["BUILD"], setup_id=baseline["setup_id"],
                      wizard_sources={name: digest(source / name) for name in SETUP_SOURCES},
                      install_notices=notice_hashes, end_user_agreement={"EULA.md": digest(agreement)},
                      runtime=baseline["runtime_build"], base_game_sha256=baseline["archive"],
                      golden_setup=baseline["kit_files"][BASE_SETUP]["sha256"],
                      checked_desktop_files=len(desktop_files), checked_setup_files=len(actual_files),
                      checked_maintenance_files=len(wizard_files), embedded_maintenance_sha256=rebuilt_wizard,
                      recovery_contains_this_maintenance=True, compiler_sha256=digest(compiler),
                      artifacts={name: {"bytes": (output / name).stat().st_size, "sha256": digest(output / name)}
                                 for name in (BASE_SETUP, RECOVERY)})
        (output / "verified.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
    subprocess.run([sys.executable, "-B", HERE / "qualify.py", "--output", output, "--tools", tools], check=True)


if __name__ == "__main__":
    main()
