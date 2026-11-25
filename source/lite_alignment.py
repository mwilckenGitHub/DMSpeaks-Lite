"""Resolve the local aligner used for fresh player-name audio."""

import os
import sys
from shutil import which
from pathlib import Path
from socket import gethostname

from pocketsphinx_phonemes import select_profile


PATHS = ("executable", "acoustic_model", "dictionary")
REQUIRED = (*PATHS, "profile_id")
PACKAGED_RUNTIME = Path(__file__).resolve().parent.parent / "runtime/pocketsphinx"


def packaged_alignment(root, executable):
    return dict(executable=str(root / executable), acoustic_model=str(root / "en-us"),
                dictionary=str(root / "cmudict-en-us.dict"), profile_id="5.1.1 cmudict en-US",
                language="en-US", phone_set="ARPAbet")


def homebrew_alignment():
    tool = which("pocketsphinx")
    prefixes = [Path("/opt/homebrew/opt/cmu-pocketsphinx"), Path("/usr/local/opt/cmu-pocketsphinx")]
    if tool:
        prefixes.insert(0, Path(tool).resolve().parents[1])
    for prefix in prefixes:
        model = prefix / "share/pocketsphinx/model/en-us"
        executable = prefix / "bin/pocketsphinx"
        acoustic_model, dictionary = model / "en-us", model / "cmudict-en-us.dict"
        if executable.is_file() and acoustic_model.is_dir() and dictionary.is_file():
            return dict(executable=str(executable), acoustic_model=str(acoustic_model),
                        dictionary=str(dictionary), profile_id="5.1.1 cmudict en-US",
                        language="en-US", phone_set="ARPAbet")
    raise ValueError("Install CMU PocketSphinx with Homebrew to verify player-name audio.")


def alignment_profile(parser):
    settings = dict(select_profile(parser, "en-US") or {})
    executable = "pocketsphinx.exe" if os.name == "nt" else "pocketsphinx"
    if not any(settings.get(key) for key in REQUIRED):
        if os.name == "nt" or (PACKAGED_RUNTIME / executable).is_file():
            defaults = packaged_alignment(PACKAGED_RUNTIME, executable)
        elif sys.platform == "darwin":
            defaults = homebrew_alignment()
        else:
            defaults = {}
        settings = {**defaults, **{key: value for key, value in settings.items() if value}}
    if not all(settings.get(key) for key in REQUIRED):
        raise ValueError("Configure an English PocketSphinx profile before fetching new audio.")
    for key in PATHS:
        path = Path(settings[key]).expanduser()
        if not (path.is_dir() if key == "acoustic_model" else path.is_file()):
            raise ValueError("The PocketSphinx profile points to missing local files.")
    if gethostname().casefold().startswith("spark") and not settings.get("inference_lease"):
        raise ValueError("The Spark PocketSphinx profile needs its approved shared inference lease.")
    return settings
