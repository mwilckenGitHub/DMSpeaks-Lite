"""Verify tracking and ownership with the rebuilt release's own Windows maintenance runtime."""
import argparse
import json
import runpy
import struct
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from build import BASE_SETUP, RECOVERY, GAME, HERE, extract, require, read, digest


def pe_machine(executable):
    with executable.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError(f"Missing PE header in {executable}")
        stream.seek(0x3c)
        offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(offset)
        if stream.read(4) != b"PE\0\0":
            raise ValueError(f"Invalid PE header in {executable}")
        return struct.unpack("<H", stream.read(2))[0]


def qualify(folder, tools):
    receipt = read(folder / "verified.json")
    seven = tools / "7zip-22.01/7z.exe"
    checks = runpy.run_path(str(HERE.parents[1] / "qa/stability/installer/check_last_shell.py"))
    for name in (BASE_SETUP, RECOVERY):
        require(folder / name, receipt["artifacts"][name]["sha256"])
        if pe_machine(folder / name) != 0x8664:
            raise ValueError(f"Expected Windows x64 PE: {name}")
    with TemporaryDirectory(prefix="tracking-", dir=folder) as work:
        work = Path(work)
        extract(seven, folder / RECOVERY, work / "recovery")
        wizard = work / "recovery/$PLUGINSDIR/maintenance.exe"
        require(wizard, receipt["embedded_maintenance_sha256"])
        if pe_machine(wizard) != 0x8664:
            raise ValueError("Embedded wizard must be Windows x64.")
        extract(seven, wizard, work / "maintenance")
        plugins = work / "maintenance/$PLUGINSDIR"
        pins = {name: digest(plugins / name) for name in checks["SUPPORT"]}
        cases = {}
        for label, changed in (("release_files", False), ("changed_and_user_files", True)):
            cases[label] = checks["run_case"](label, work, plugins, wizard, receipt["setup_id"], pins, changed, True)
        if not all(item["passed"] for case in cases.values() for item in case["uninstall"]):
            raise ValueError(f"Isolated maintenance checks failed: {cases}")
        checked = work / "setup"
        extract(seven, folder / BASE_SETUP, checked, "$2/source/*")
        script = ("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                  "from open_lite import installed_build_id; import install_addon; "
                  "print(installed_build_id(Path(sys.argv[2])), install_addon.ARCHIVE_SHA256)")
        facts = subprocess.check_output([sys.executable, "-I", "-B", "-c", script, plugins, checked / "$2"],
                                        text=True).strip().split()
        if facts != [receipt["runtime"], receipt["base_game_sha256"]]:
            raise ValueError(f"Extracted companion/sidecar identities differ: {facts}")
    report = {"pe_machine": "AMD64 (0x8664) for Setup, Recovery and the embedded wizard",
              "registered_build": receipt["base_build"], "source_identity": facts[0],
              "placement_archive": {GAME: facts[1]}, "setup_id": receipt["setup_id"],
              "maintenance_sha256_recorded_and_read_back_in_private_hive": receipt["embedded_maintenance_sha256"],
              "private_hive_behavior": cases,
              "scope": "Real released Windows helper commands in isolated app hives and temporary game folders. "
                       "NSIS shell and UI were compiled and extracted. The UI was not run on a real player installation."}
    (folder / "tracking-verified.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Verified runtime and game identities, private registration, cleanup, retry and personal-file retention.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tools", required=True, type=Path)
    args = parser.parse_args()
    qualify(args.output.resolve(), args.tools.resolve())
