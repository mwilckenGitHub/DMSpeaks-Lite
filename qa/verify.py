"""Exercise the public source package using disposable files and no external services."""

import argparse
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    "fetch_timing_history", "profile_archive_batch", "fetch_provider_cooldown",
    "fetch_restart", "fetch_request_races", "fetch_storage_crash",
    "fetch_source_availability", "fetch_name_retakes", "saved_clip_bounds",
    "skyborne_profiles", "fetch_progress_status", "profile_session",
)


def verify_archive(work):
    sys.path.insert(0, str(ROOT / "packaging"))
    from build_core import MODULES, build

    first, second = work / "core.zip", work / "second.zip"
    build(first)
    build(second)
    assert sha256(first.read_bytes()).digest() == sha256(second.read_bytes()).digest()
    folder = work / "core"
    with ZipFile(first) as archive:
        assert set(archive.namelist()) == ({name + ".py" for name in MODULES}
            | {"formats/phonetic-join-profiles-1.json", "LICENSE", "requirements.txt"})
        archive.extractall(folder)
    program = (
        "import importlib,sys; sys.path.insert(0,sys.argv[1]);"
        "mods=[importlib.import_module(n) for n in sys.argv[2:]];"
        "from leading_word import leading_word;"
        "from phonetic_join_profiles import load_profiles;"
        "from lite_fetch_timing import npc_timings;"
        "assert leading_word('Welcome, $n.',9,[(9,11)]).text=='Welcome';"
        "assert npc_timings([{'npc':'Guide','elapsed_seconds':2}])[0]['elapsed_seconds']==2;"
        "assert load_profiles()"
    )
    subprocess.run([sys.executable, "-I", "-B", "-c", program, folder, *MODULES],
                   cwd=folder, check=True)
    print("Core ZIP reproduced and imported in isolation, including phonetic profiles.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-only", action="store_true")
    options = parser.parse_args()
    with TemporaryDirectory() as temporary:
        verify_archive(Path(temporary))
    if options.core_only:
        return
    sys.path[:0] = [str(ROOT / "qa"), str(ROOT / "source")]
    from lite_share_playback import probe_audio
    import soundfile as sf

    samples, rate = sf.read(BytesIO(probe_audio()))
    assert rate == 22050 and len(samples) == 1323 and not samples.any()
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromNames(SUITES))
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
