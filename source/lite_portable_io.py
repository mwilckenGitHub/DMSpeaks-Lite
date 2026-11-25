"""Bounded local I/O for the standalone player-name workflow."""

from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from shutil import copyfileobj
from tempfile import NamedTemporaryFile, TemporaryDirectory

import soundfile as sf
from guide_bindings import RATE


WINDOWS_EXTENDED = "\\\\?\\"
WINDOWS_EXTENDED_UNC = WINDOWS_EXTENDED + "UNC\\"


def filesystem_path(path):
    """Use Windows extended-length syntax for absolute companion file paths."""
    path = Path(path)
    if os.name != "nt" or not path.is_absolute():
        return path
    name = str(path)
    if name.startswith((WINDOWS_EXTENDED, "\\\\.\\")):
        return path
    name = os.path.normpath(name)
    if name.startswith("\\\\"):
        return Path(WINDOWS_EXTENDED_UNC + name[2:])
    return Path(WINDOWS_EXTENDED + name)


def path_key(path, *, resolve=True):
    """Keep cache keys compatible with receipts written before extended paths."""
    native = filesystem_path(path)
    name = str(native.resolve() if resolve else native)
    if os.name != "nt":
        return name
    if name[:len(WINDOWS_EXTENDED_UNC)].upper() == WINDOWS_EXTENDED_UNC:
        return "\\\\" + name[len(WINDOWS_EXTENDED_UNC):]
    prefix = len(WINDOWS_EXTENDED)
    if name.startswith(WINDOWS_EXTENDED) and name[prefix + 1:prefix + 3] == ":\\":
        return name[prefix:]
    return name


def digest(data):
    return sha256(data).hexdigest()


def evidence_matches(members, name, data, expected):
    """Accept exact original evidence or its build-pinned portable copy."""
    actual = digest(data)
    if actual == expected:
        return True
    projected = getattr(members, "projected_evidence", None)
    return projected is not None and projected(name) == [expected, actual]


def archive_names_under(members, prefix):
    indexed_lookup = getattr(members, "names_under", None)
    if indexed_lookup is not None:
        return indexed_lookup(prefix)
    return (name for name in members if name.startswith(prefix))


def file_hash(path):
    with path.open("rb") as stream:
        h = sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
        return h.hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def stock_binding(manifest):
    if "analysis_base_sha256" in manifest:
        return manifest.get("reference_spoken")
    return manifest.get("original_spoken")


def render_fingerprint(pcm, seed=None, analysis=None, caption=None, reference=None, native=None, delivery=None):
    parts = [pcm, seed, analysis, caption]
    if reference is not None:
        parts.append({"reference_contract": reference})
    if native is not None:
        parts.append({'native_source':native})
    if delivery is not None:
        parts.append({'delivery_contract':delivery})
    return digest(json.dumps(parts, sort_keys=True).encode("utf-8"))


def manifest_render_fingerprint(manifest):
    delivery = None
    seal = manifest.get('delivery_contract_sha256', manifest.get('delivery_contract_sha'))
    if seal is not None:
        import re
        if (not isinstance(seal, str) or not re.fullmatch(r'[0-9a-f]{64}', seal)
                or manifest.get('delivery_contract_sha', seal) != seal):
            raise ValueError('The delivery contract identity is invalid or conflicting.')
        delivery = dict(sha256=seal, profile=manifest.get('delivery_profile'),
                        format=manifest.get('delivery_clock_format'))
    source = manifest.get('source_pcm_sha256')
    if source is None and delivery is not None:
        native = manifest.get('source_native_manifest_sha256')
        if not isinstance(native, str) or not re.fullmatch(r'[0-9a-f]{64}', native):
            raise ValueError('A paced delivery needs its admitted native manifest identity.')
        source = {'native_manifest_sha256':native}
    if source is None:raise ValueError('The source PCM identity is missing.')
    return render_fingerprint(source, manifest.get("companion_seed"),
                              manifest.get("analysis_base_sha256"), manifest.get("reconstructed_caption"),
                              manifest.get("reference_contract"),manifest.get('native_source'),delivery)


def cache_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stage = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            stage.replace(path)
        finally:
            stage.unlink(missing_ok=True)


def cache_file(path, source, *, reuse_sealed_storage=False):
    """Copy a file atomically, with optional reuse of replace-only cache files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if reuse_sealed_storage and not source.is_symlink() and not path.is_symlink():
        try:
            if path.is_file():
                if path.samefile(source):
                    return
                with TemporaryDirectory(dir=path.parent, prefix=".sealed-cache-") as temporary:
                    stage = filesystem_path(Path(temporary) / "linked")
                    os.link(source, stage)
                    stage.replace(path)
            else:
                os.link(source, path)
        except OSError:
            # Some profile filesystems do not support hard links. In that case
            # copy within the destination directory and replace atomically.
            pass
        else:
            return
    with source.open("rb") as contents, NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stage = Path(stream.name)
        try:
            copyfileobj(contents, stream, 1024 * 1024)
            stream.close()
            stage.replace(path)
        finally:
            stage.unlink(missing_ok=True)


def save(path, value):
    cache_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def validate_occurrences(route, *, complete_names=False, gender_branch=None):
    """Check delimiter ownership separately from its PCM and playback clocks."""
    counts = {}
    fallbacks = set()
    names = None
    for part in route:
        kind = part["kind"]
        if kind == "fixed":
            continue
        occurrence = part.get("occurrence")
        if type(occurrence) is not int or occurrence != counts.get(kind, 0) + 1:
            raise ValueError(f"Dialogue {kind} occurrences must be unique and in source order.")
        counts[kind] = occurrence
        declared = part.get("expected_values")
        choices = part.get("choices")
        if (not isinstance(declared, list) or not all(isinstance(value, str) and value.strip() for value in declared)
                or not isinstance(choices, dict)):
            raise ValueError(f"Dialogue {kind} occurrence {occurrence} has undeclared choices.")
        expected = {value.casefold() for value in declared}
        if choices.keys() - expected:
            raise ValueError(f"Dialogue {kind} occurrence {occurrence} has undeclared choices.")
        fallback = part.get("fallback")
        if not isinstance(fallback, str) or fallback not in choices:
            raise ValueError("A dialogue occurrence lacks its declared fallback.")
        if kind in {"name", "race", "class"}:
            if fallback != "adventurer":
                raise ValueError(f"Dialogue {kind} occurrence {occurrence} needs its contextual Adventurer fallback.")
            clip = choices[fallback]
            filename = clip.get("file") if isinstance(clip, dict) else None
            if not isinstance(filename, str) or not filename:
                raise ValueError("A contextual fallback has no valid clip filename.")
            if filename in fallbacks:
                raise ValueError("Two dialogue occurrences share the same contextual fallback clip.")
            fallbacks.add(filename)
        if kind == "gender":
            branches = part.get("value_by_player_sex")
            if branches is None and gender_branch in {"m", "f"} and expected == choices.keys() == {fallback}:
                continue
            if (not isinstance(branches, dict) or set(branches) != {"2", "3"}
                    or not all(isinstance(value, str) for value in branches.values())
                    or set(branches.values()) != choices.keys()):
                raise ValueError("The dialogue must preserve both declared gender branches.")
        if complete_names and kind == "name":
            present = choices.keys() - {fallback}
            if names is not None and present != names:
                raise ValueError("A personalized name must cover every Name occurrence before packaging.")
            names = present


def clip_binding(clip):
    return clip["sha256"], clip["samples"]


def validate_routes(folder, manifest, scratch, *, complete_names=False, clip_bytes=None, sealed_clips=None):
    from lite_clip_bounds import saved_cuts

    used = set()
    gender_branch = manifest.get("source_id", "").rsplit(":", 1)[-1]
    for route in (manifest["sequence"], *manifest.get("character_sequences", {}).values()):
        validate_occurrences(route, complete_names=complete_names, gender_branch=gender_branch)
        cursor = 0
        for part in route:
            start, end = part["source_samples"]
            if start != cursor or end <= start:
                raise ValueError("Dialogue clips do not cover consecutive source samples.")
            cursor = end
            if part["kind"] == "fixed":
                options = [part["clip"]]
                if options[0]["samples"] != end - start:
                    raise ValueError("A fixed clip differs from the original source clock.")
            else:
                options = part["choices"].values()
            for clip in options:
                saved_cuts(clip)
                filename = clip["file"]
                if Path(filename).name != filename or "/" in filename or "\\" in filename:
                    raise ValueError("A dialogue clip path leaves the prepared package.")
                used.add(filename)
                if sealed_clips is not None and sealed_clips.get(filename) == clip_binding(clip):
                    continue
                if clip_bytes is None:
                    audio_source = folder / filename
                    if audio_source.is_symlink():
                        raise ValueError("A dialogue clip may not link outside its package.")
                    checksum = file_hash(audio_source)
                else:
                    if filename not in clip_bytes:
                        raise FileNotFoundError(filename)
                    contents = clip_bytes[filename]
                    checksum = digest(contents)
                    audio_source = BytesIO(contents)
                if checksum != clip["sha256"]:
                    raise ValueError(f"The staged Ogg changed: {filename}")
                try:
                    audio = sf.info(audio_source)
                except sf.LibsndfileError as error:
                    raise ValueError(f"The staged Ogg is unreadable: {filename}") from error
                if (audio.format, audio.subtype, audio.samplerate, audio.channels, audio.frames) != (
                        "OGG", "VORBIS", RATE, 1, clip["samples"]):
                    raise ValueError(f"The staged Ogg has the wrong sample clock: {filename}")
        if cursor != manifest["source_samples"]:
            raise ValueError("The source route ends at the wrong sample.")
    return used



@contextmanager
def hold(folder, filename="project.lock"):
    from project_recovery import WorkingCopyLease

    lease = WorkingCopyLease(folder, filename)
    try:
        yield
    finally:
        lease.close()
