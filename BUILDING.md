# Building DMSpeaks Lite

## Run the source checks

Use Python 3.11 or newer. The following commands run from the repository root on Windows.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -r source/requirements.txt
.\.venv\Scripts\python -B qa/verify.py
```

The tests build a reproducible core archive, import its modules in a separate Python process, and run the companion's timing, profile, fetch, clip placement and restart tests in temporary directories. They use synthetic speech input and temporary character profiles. The checkout contains the Lua add-on integration under `source/lite_addon` and the Fetch Settings browser application under `source/lite_web`.

## Package the headless functions

```powershell
py -3 -B packaging/build_core.py dist/dmspeaks-core.zip
```

Extract the ZIP into your Python application and install its `requirements.txt`. It contains the timing, leading-word, phoneme profile, identity, race and audio-boundary modules and their dependencies. Its phonetic profile data travels with the functions. The player UI, installer and example corpora are separate. For a core-only check, install `packaging/core-requirements.txt` and run `python -B qa/verify.py --core-only`. Remove or rename an existing output ZIP before choosing the same output path for a new build.

## Use the Windows companion from source

The player-facing entry point is `source/wow_fetch_desktop.py`. Its first argument is the WoW installation containing `Interface` and `WTF`. The companion reads the installed DMSpeaks managed playback add-ons, matching name catalog and metadata. A licensed, version-matched installation of these assets and Spoken is required to run an in-game name fetch. Installing Spoken alone supplies only part of that data.

Put FFmpeg and FFprobe on your PATH. Fresh speech also needs the PocketSphinx command-line aligner, a matching model and pronunciation dictionary, and a configured speech provider. The example provider file contains the public provider options. The companion creates and stores your personal configuration in its own data directory. [PocketSphinx setup](source/third_party/pocketsphinx.md)

```powershell
.\.venv\Scripts\python -B source/launch.py --check
.\.venv\Scripts\python -B source/wow_fetch_desktop.py --help
.\.venv\Scripts\python -B source/wow_fetch_desktop.py "C:\Games\World of Warcraft\_forever_"
```

The source UI uses project-drawn waveform icons. Where an installation has a matching character portrait the companion can display it. Otherwise the source UI uses its generic icon. Playback on shared storage uses a short digital-silence probe. A source installation can encode that probe locally.

## Build the historical Windows Setup and Recovery

The NSIS recipes and DMSpeaks installation helpers are in `installer/`. The native build reproduces the registered Beta 12 base. It checks supplied build inputs by SHA-256, compiles Setup and Recovery, compares the extracted payloads and exercises install tracking in disposable Windows registry hives. This recipe uses the frozen Beta 12 base archive. Subsequent companion and content updates have their own versions.

The builder takes two explicit directories. The tools directory supplies `nsis-3.09/Bin/makensis.exe`, 7-Zip 22.01 and their matching support files. The inputs directory supplies the frozen installer resources, its desktop payload, matching runtime, original game-media archives, artwork and maintenance support. Those inputs must be obtained with the appropriate rights. The build checks the exact relative names and expected hashes in the lockfile. Use a new output directory. [Pinned input inventory](packaging/windows-setup/native-lock.json)

```powershell
py -3 -B packaging/windows-setup/build.py --inputs "D:\BuildInputs\Setup-6cfd18e9" --tools "D:\BuildTools" --output "D:\Builds\Lite-Beta12"
```

A successful native build writes `verified.json` and `tracking-verified.json` beside its two executables. The core build and source tests can run independently of these payloads. The two public downloads on the project page update supported existing Lite installations. A new player's full installation also requires the coordinated game-content package. [Download instructions](README.md) · [Third-party terms](THIRD_PARTY.md)
