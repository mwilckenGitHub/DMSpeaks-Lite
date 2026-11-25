"""Exercise the released final-client cleanup commands in a private Windows profile and registry."""
import argparse
import configparser
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import winreg


APP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\DMSpeaksLite"
SUPPORT = ("open_lite.py", "install_addon.py", "addon-files.txt.gz", "stock-files.json.gz", "DMSpeaks-Orc.ico")
SETUP = "DMSpeaks Lite - WOW Setup.exe"
RECOVERY = "DMSpeaks Uninstall Recovery.exe"
BUILD = "5996a8fb357ae6741b77"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_in_hive(hive, tool, argv):
    """Redirect HKCU in this process to a file-backed application hive before running release code."""
    registry = ctypes.WinDLL("advapi32")
    registry.RegLoadAppKeyW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.HANDLE),
                                        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
    registry.RegOverridePredefKey.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    registry.RegCloseKey.argtypes = (wintypes.HANDLE,)
    handle = wintypes.HANDLE()
    error = registry.RegLoadAppKeyW(str(hive), ctypes.byref(handle), winreg.KEY_ALL_ACCESS, 1, 0)
    if error:
        raise ctypes.WinError(error)
    hkcu = wintypes.HANDLE(ctypes.c_int32(0x80000001).value)
    try:
        error = registry.RegOverridePredefKey(hkcu, handle)
        if error:
            raise ctypes.WinError(error)
        try:
            if tool == "init":
                # Create the revision directly under the file handle; confirm HKCU really points there.
                with winreg.CreateKey(int(handle.value), APP_KEY) as k:
                    winreg.SetValueEx(k, "CleanupRevision", 0, winreg.REG_SZ, argv[0])
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY) as k:
                    assert winreg.QueryValueEx(k, "CleanupRevision")[0] == argv[0]
            elif tool == "check-registration":
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY) as k:
                    assert winreg.QueryValueEx(k, "MaintenanceSHA256")[0] == argv[0]
            else:
                sys.argv = [tool, *argv]
                runpy.run_path(tool, run_name="__main__")
        finally:
            assert registry.RegOverridePredefKey(hkcu, None) == 0
    finally:
        assert registry.RegCloseKey(handle) == 0


def extract(seven, archive, target):
    subprocess.run([seven, "x", "-y", "-o"+str(target), archive], check=True,
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def run_case(name, base, plugins, executable, revision, source_pins, changed, reconcile):
    case = base / name
    game = case / "Game"
    (game / "Interface/AddOns").mkdir(parents=True)
    env = dict(os.environ, USERPROFILE=str(case/"User"), LOCALAPPDATA=str(case/"Local"),
               APPDATA=str(case/"Roaming"), OneDrive=str(case/"User/OneDrive"))
    product = case / "Local/DMSpeaksLite"
    build = product / "Builds" / BUILD
    build.mkdir(parents=True)
    maintenance = product / "Maintenance"
    maintenance.mkdir()
    for item in SUPPORT:
        shutil.copyfile(plugins/item, maintenance/item)
        assert digest(maintenance/item) == source_pins[item]
    installed_exe = maintenance / "DMSpeaks-Maintenance.exe"
    marker = game / "Interface/.dmspeaks-setup-version.txt"
    marker.write_bytes((revision + "\r\n").encode("ascii"))
    lock = build / ".beta-update.lock"
    lock.touch()
    keep = b"independent fixture user content"
    retained_support = bytearray((maintenance/"open_lite.py").read_bytes())
    retained_support[-1] ^= 1
    if changed:
        for item in (marker, lock, maintenance/"notes.txt"):
            item.write_bytes(keep)
        (maintenance/"open_lite.py").write_bytes(retained_support)
    python = plugins/"cleanup/python.exe"
    def call(tool, *args):
        return subprocess.run([python, "-I", "-B", Path(__file__).resolve(), "--hive", case/"registry.hiv",
                               tool, *args], env=env, cwd=case, capture_output=True,
                              creationflags=subprocess.CREATE_NO_WINDOW, timeout=90)
    def checked(tool, *args):
        result = call(tool, *args)
        assert result.returncode == 0, (str(tool), args, result.stdout, result.stderr)
    shell = plugins / "cleanup/shell_maintenance.py"
    def shell_mode(mode):
        checked(shell, mode, build, game, plugins)
    orphan = None
    if reconcile:
        probe = call(shell, "reconcile-orphan", build, game, plugins)
        assert (probe.returncode == 0) == (not changed), (probe.stdout, probe.stderr)
        assert build.exists() == changed
        if changed:
            assert lock.read_bytes() == keep and (maintenance/"open_lite.py").read_bytes() == retained_support
        else:
            assert all(digest(maintenance/n) == source_pins[n] for n in SUPPORT)
            build.mkdir()
            lock.touch()
        orphan = {"accepted":probe.returncode == 0,"build_retained":changed,"shell_bytes_preserved":True}
    shutil.copyfile(executable, installed_exe)
    checked("init", revision)
    shell_mode("record")
    checked("check-registration", digest(executable))
    if changed:
        # A same-name replacement at the registered executable's path belongs to the user.
        installed_exe.write_bytes(keep)
    results = []
    for attempt in ("first", "retry"):
        ini = case / (attempt + ".ini")
        checked(plugins/"cleanup/prune_runtimes.py", "--mode", "uninstall", "--build", BUILD,
                "--builds", build.parent, "--game", game, "--result", ini)
        parser = configparser.ConfigParser()
        parser.read(ini, encoding="utf-16")
        assert dict(parser["clients"]) == {"shared": "0"}
        shell_mode("remove-marker")
        shell_mode("remove-last")
        # The wizard finishes with non-recursive RMDir. An owned file left by the helper blocks it.
        for path in (maintenance, build.parent, product):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        files = sorted(p.relative_to(product).as_posix() for p in product.rglob("*") if p.is_file())
        expected = (["Builds/"+BUILD+"/.beta-update.lock", "Maintenance/DMSpeaks-Maintenance.exe",
                     "Maintenance/notes.txt", "Maintenance/open_lite.py"] if changed else [])
        expected_bytes = all((product/p).read_bytes() == (retained_support if p == "Maintenance/open_lite.py" else keep)
                             for p in expected if (product/p).is_file())
        passed = (files == sorted(expected) and product.exists() == changed and marker.exists() == changed
                  and (not changed or marker.read_bytes() == keep) and expected_bytes)
        results.append({"attempt":attempt, "passed":passed, "remaining":files, "product_exists":product.exists(),
                        "setup_marker_exists":marker.exists()})
    return {"orphan_reconciliation":orphan, "uninstall":results}


def qualification(args):
    receipt = json.loads(args.build_receipt.read_bytes())
    released = receipt.get("kit_files") or receipt["kit"]
    helper_pins = receipt.get("helper") or {
        n:receipt["inputs"][n] for n in ("prune_runtimes.py", "runtime-files.json.gz")
    }
    kit_pins = {n:digest(args.kit/n) for n in (SETUP, RECOVERY)}
    assert kit_pins == {n:released[n]["sha256"] for n in kit_pins}
    assert kit_pins[SETUP] == args.setup_sha
    revision = receipt["setup_id"]
    assert len(revision) == 64 and bytes.fromhex(revision)
    with TemporaryDirectory(prefix="dms-final-client-shell-") as tmp:
        base = Path(tmp)
        extract(args.seven_zip, args.kit/RECOVERY, base/"bundle")
        executable, = list((base/"bundle").rglob("Maintenance.exe"))
        extract(args.seven_zip, executable, base/"maintenance")
        plugins, = [p.parent for p in (base/"maintenance").rglob("legacy.ico")]
        source_pins = {n:digest(plugins/n) for n in SUPPORT}
        for n in ("install_addon.py", "stock-files.json.gz"):
            assert source_pins[n] == receipt["inputs"][n]
        static = {"open_lite.py":"8ed395be6cbb7cfcba4886eb1bba580fb0cc49513da5b45d987665bf6ce62005",
                  "addon-files.txt.gz":"4cfd878b222cf2c48d61bd56428fd398259eac09fbece176e3423b7636185570",
                  "DMSpeaks-Orc.ico":"bf530f4e158ecaf512af2b72060f4c7f24c51483ac2bf5ec95950a26e48a73bd"}
        for n, expected in static.items():
            assert source_pins[n] == expected
        for n, expected in {"shell_maintenance.py":receipt["inputs"]["shell_maintenance.py"],
                            **helper_pins}.items():
            assert digest(plugins/"cleanup"/n) == expected
        active_shell = receipt["inputs"]["shell_maintenance.py"]
        active_prune = helper_pins["prune_runtimes.py"]
        if args.prebuild_shell:
            assert args.prebuild_prune and args.shell_sha and args.prune_sha
            for file, expected in ((args.prebuild_shell,args.shell_sha),(args.prebuild_prune,args.prune_sha)):
                assert digest(file) == expected
                shutil.copyfile(file, plugins/"cleanup"/file.name)
            active_shell, active_prune = args.shell_sha, args.prune_sha
        cases = {name:run_case(name, base, plugins, executable, revision, source_pins, changed, args.reconcile)
                 for name, changed in (("exact_release_files",False), ("changed_and_unknown_files",True))}
        passed = all(result["passed"] for case in cases.values() for result in case["uninstall"])
        report = {"passed":passed, "kit":kit_pins, "setup_id":revision, "maintenance_sha256":digest(executable),
                  "support_sha256":source_pins, "shell_sha256":active_shell,
                  "prune_sha256":active_prune, "prebuild_sources_applied":bool(args.prebuild_shell),
                  "registry":"private file-backed hive",
                  "interface":"released maintenance interpreter and CLI, simulated final nonrecursive directory removal",
                  "current_build":BUILD, "cases":cases}
    args.report.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"passed":passed, "output":str(args.report)}))
    return 0 if passed else 1


if __name__ == "__main__":
    if sys.argv[1] == "--hive":
        run_in_hive(Path(sys.argv[2]), sys.argv[3], sys.argv[4:])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--kit", type=Path, required=True)
        parser.add_argument("--build-receipt", type=Path, required=True)
        parser.add_argument("--setup-sha", required=True)
        parser.add_argument("--report", type=Path, required=True)
        parser.add_argument("--reconcile", action="store_true", help="Exercise Setup's orphan admission before teardown")
        parser.add_argument("--prebuild-shell", type=Path)
        parser.add_argument("--prebuild-prune", type=Path)
        parser.add_argument("--shell-sha")
        parser.add_argument("--prune-sha")
        parser.add_argument("--seven-zip", type=Path,
                            default=Path(os.environ["ProgramFiles"])/"NVIDIA Corporation/NVIDIA app/7z.exe")
        raise SystemExit(qualification(parser.parse_args()))
