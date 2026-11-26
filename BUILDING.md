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

The functions archive includes the same ordered Fish reference and English name-audio
checks used by the DMSpeaks authoring tools. A fetch supplies the configured voice
reference first and the complete original dialogue second when available. The
acoustic check uses PocketSphinx and its English model and dictionary. Pronouncing
names outside that dictionary can use eSpeak NG. The source checks use synthetic
recordings for reference order, placeholder timing and selection of the recorded
source when several actors share a quest stage.

The player-facing entry point is `source/wow_fetch_desktop.py`. Its first argument is the WoW installation containing `Interface` and `WTF`. The companion reads the installed DMSpeaks managed playback add-ons, matching name catalog and metadata. A licensed, version-matched installation of these assets and Spoken is required to run an in-game name fetch. Installing Spoken alone supplies only part of that data.

Put FFmpeg and FFprobe on your PATH. Fresh speech also needs the PocketSphinx command-line aligner, a matching model and pronunciation dictionary, and a configured speech provider. The example provider file contains the public provider options. The companion creates and stores your personal configuration in its own data directory. [PocketSphinx setup](source/third_party/pocketsphinx.md)

```powershell
.\.venv\Scripts\python -B source/launch.py --check
.\.venv\Scripts\python -B source/wow_fetch_desktop.py --help
.\.venv\Scripts\python -B source/wow_fetch_desktop.py "C:\Games\World of Warcraft\_forever_"
```

The source UI uses project-drawn waveform icons. Where an installation has a matching character portrait the companion can display it. Otherwise the source UI uses its generic icon. Playback on shared storage uses a short digital-silence probe. A source installation can encode that probe locally.

## Build Windows Setup with separate playback packs

Use the verified compact Beta 12 kit and its build receipt as the input to `packaging/windows-setup/build_spoken_release.py`. Pass the kit directory as `--previous`, the SHA-256 of its `verified.json` as `--previous-sha256`, the Windows tools directory as `--tools`, and a new directory as `--output`. For online Setup, pass the GitHub tag that will host the media ZIPs as `--media-tag` (`--release-tag` is an alias). The output receipt identifies each asset by size and SHA-256.

When the six ZIPs match an existing published media set by SHA-256, build Setup with that media tag and attach just the new installer to its software release. Keep the media tag and assets available while installers reference them. If a ZIP changes, publish a matching set and point Setup at its tag.

To include reviewed audio joins with the same release media, supply the measured overlay ZIP with `--join-overlay` and its SHA-256 with `--join-overlay-sha256`. Setup verifies the source clips and the overlay's file hashes. The build receipt lists the installed repair files.

For Playback in Context, pass the matching projection JSON as `--recording-context`. Generate it in the Lua replay environment with `prepare_recording_projection.py --media <media folder> --join-overlay <overlay ZIP> --output <projection JSON>`. The companion pins the projection checksum, and Setup verifies it against the exact game catalog, stock registrations, and audio repair bundle before packaging.

Setup requires Spoken in the selected game folder. Its voice pack page lets the player choose which English Lite packs to install. The common Lite files and name data are included in every installation. Existing Lite packs are kept when their boxes are cleared. Setup checks local ZIPs first, then downloads the missing common files and selected packs from the release supplied at build time.

Before pointing the README at a new release, verify the public installer and the ZIPs at its media tag against the build receipt. Run the downloadable Setup with an empty cache and no adjacent ZIPs. Check that a one-pack selection downloads just the common files and that pack, and that an all-packs selection can install every archive.

For a silent install, use `/VOICEPACKS=0` for the common files alone, or add the values of the packs you want. Alliance is 1, Horde is 2, Shared is 4, and Gossip is 8. For example, `/VOICEPACKS=5` installs Alliance and Shared. Silent invocations without this option continue to select the packs that match the installed Spoken voice packs.

## Build the historical Windows Setup and Recovery

The NSIS recipes and DMSpeaks installation helpers are in `installer/`. The native build uses the registered Beta 12 base payload. Setup requires acceptance of the [Lite end-user agreement](EULA.md) and then displays the MIT license, Spoken credits and runtime notices before game-folder selection. An unattended install uses `/S /ACCEPTEULA` after the operator has accepted the agreement. The builder checks supplied inputs by SHA-256, compiles Setup and Recovery, compares the extracted payloads and exercises install tracking in disposable Windows registry hives. This recipe uses the frozen Beta 12 base archive. Subsequent companion and content updates have their own versions.

The source checkout includes the three release notices used by default. To build with the notices for another authorized distribution, provide a directory containing `LICENSE`, `THIRD_PARTY.md` and `THIRD-PARTY-RUNTIMES.md` with `--notices`. The build records their hashes. [Installer notices](packaging/windows-setup/notices)

The builder takes two explicit directories. The tools directory supplies `nsis-3.09/Bin/makensis.exe`, 7-Zip 22.01 and their matching support files. The inputs directory supplies the frozen installer resources, its desktop payload, matching runtime, original game-media archives, artwork and maintenance support. Those inputs must be obtained with the appropriate rights. The build checks the exact relative names and expected hashes in the lockfile. Use a new output directory. [Pinned input inventory](packaging/windows-setup/native-lock.json)

```powershell
py -3 -B packaging/windows-setup/build.py --inputs "D:\BuildInputs\Setup-6cfd18e9" --tools "D:\BuildTools" --output "D:\Builds\Lite-Beta12"
```

A successful native build writes `verified.json` and `tracking-verified.json` beside its two executables. The core build and source tests can run independently of these payloads. The two public downloads on the project page update supported existing Lite installations. A new player's full installation also requires the coordinated game-content package. [Download instructions](README.md) Â· [Third-party terms](THIRD_PARTY.md)
