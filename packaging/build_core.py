"""Package the standalone source for timing, joins, identity and PCM functions."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


HERE = Path(__file__).resolve().parents[1]
MODULES = (
    "dialogue_language", "engine_limits", "guide_bindings", "leading_word",
    "lite_clip_bounds", "lite_context_routes", "lite_fetch_timing",
    "lite_legacy_clip_bounds", "lite_portable_io", "lite_races",
    "lite_source_identity", "pcm_input", "phoneme_alignment",
    "phonetic_join_profiles", "project_recovery", "quiet_boundaries", "vorbis_pcm",
)


def build(destination):
    source = HERE / "source"
    files = {name + ".py": source / (name + ".py") for name in MODULES}
    profile = "formats/phonetic-join-profiles-1.json"
    files.update({profile: source / profile, "LICENSE": HERE / "LICENSE",
                  "requirements.txt": HERE / "packaging" / "core-requirements.txt"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A stable date and member order allow independent builds to be compared.
    with ZipFile(destination, "x", compression=ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            entry = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New path for the core source ZIP")
    build(parser.parse_args().destination)
