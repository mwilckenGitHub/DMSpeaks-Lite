"""Build the small add-on overlay for reviewed variable-audio joins."""

from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from zipfile import ZipFile


ADDON = "DMSpeaksSpokenNames"
REPAIRS = f"{ADDON}/JoinRepairs"
MANIFEST = REPAIRS + ".json"
PROGRAM = f"{ADDON}/Playback.lua"
TOC = f"{ADDON}/{ADDON}.toc"
SHA = re.compile(r"[a-f0-9]{64}\Z")
FRAMES_LIMIT = 44100 * 300
MAX_BYTES = 32 * 1024 * 1024
MEDIA_ROOTS = {ADDON, *(f"DMSpeaksSpoken{kind}" for kind in ("Alliance", "Horde", "Shared", "Gossip"))}


def _route_source(path):
    parsed = PurePosixPath(path)
    return (parsed.as_posix() == path and len(parsed.parts) >= 3 and parsed.parts[0] in MEDIA_ROOTS
            and ".." not in parsed.parts and re.fullmatch(r"[A-Za-z0-9_./-]+\.ogg", path))


def _write_lua(routes):
    lines = ['local _, addon = ...', 'addon.infillJoinRepairs = {']
    for revision, groups in sorted(routes.items()):
        lines.append(f'    [{json.dumps(revision)}] = {{')
        for group in groups:
            replacement = json.dumps((REPAIRS + "/" + group["sha256"] + ".ogg").replace("/", "\\"))
            lines.append(f'        {{ path = {replacement}, samples = {group["samples"]}, parts = {{')
            for part in group["parts"]:
                original = json.dumps(part["path"].replace("/", "\\"))
                lines.append(f'            {{ path = {original}, samples = {part["samples"]} }},')
            lines.append('        } },')
        lines.append('    },')
    lines += ['}', '']
    return "\n".join(lines).encode("ascii")


def load_join_overlay(bundle, archives, playback):
    """Tie alternate sounds to the exact clips they replace and to the loader that uses them."""
    with ExitStack() as stack:
        zip_input = stack.enter_context(ZipFile(bundle))
        listed = zip_input.infolist()
        if (len(listed) > 512 or sum(info.file_size for info in listed) > MAX_BYTES
                or len({info.filename for info in listed}) != len(listed)):
            raise ValueError("The spoken join repair archive is too large or has duplicate files.")
        manifest = zip_input.read(MANIFEST)
        document = json.loads(manifest)
        if (not isinstance(document, dict) or set(document) != {"format", "routes"}
                or document["format"] != "dmspeaks-join-repairs-1"
                or not isinstance(document["routes"], dict) or not document["routes"]):
            raise ValueError("The spoken join repair manifest has an unexpected format.")
        sources = {}
        for archive in archives:
            source = stack.enter_context(ZipFile(archive))
            for name in source.namelist():
                sources[name] = source
        overlays = {}
        routes = document["routes"]
        for revision, groups in routes.items():
            if not SHA.fullmatch(revision) or not isinstance(groups, list) or not groups:
                raise ValueError("A spoken join repair has an invalid dialogue revision.")
            for group in groups:
                if (not isinstance(group, dict) or set(group) != {"sha256", "samples", "parts"}
                        or not isinstance(group["sha256"], str) or not SHA.fullmatch(group["sha256"])
                        or type(group["samples"]) is not int or not 0 < group["samples"] <= FRAMES_LIMIT
                        or not isinstance(group["parts"], list) or not 2 <= len(group["parts"]) <= 64):
                    raise ValueError("A spoken join repair has invalid fused audio metadata.")
                samples = 0
                for part in group["parts"]:
                    if (not isinstance(part, dict) or set(part) != {"path", "sha256", "samples"}
                            or not isinstance(part["path"], str) or not _route_source(part["path"])
                            or not isinstance(part["sha256"], str) or not SHA.fullmatch(part["sha256"])
                            or type(part["samples"]) is not int or part["samples"] <= 0):
                        raise ValueError("A spoken join repair has invalid source clip metadata.")
                    original = part["path"]
                    source = sources.get(original)
                    if source is None or sha256(source.read(original)).hexdigest() != part["sha256"]:
                        raise ValueError(f"A spoken join repair targets a different recording: {original}")
                    samples += part["samples"]
                if samples != group["samples"]:
                    raise ValueError("A spoken join repair changed the dialogue clock.")
                name = f'{REPAIRS}/{group["sha256"]}.ogg'
                if name not in overlays:
                    repaired = zip_input.read(name)
                    if not repaired.startswith(b"OggS") or sha256(repaired).hexdigest() != group["sha256"]:
                        raise ValueError(f"The repaired spoken join has different bytes: {name}")
                    overlays[name] = repaired
        if set(zip_input.namelist()) != {MANIFEST, *overlays}:
            raise ValueError("The spoken join repair archive contains unexpected files.")
        original_program = sources[PROGRAM].read(PROGRAM)
        original_toc = sources[TOC].read(TOC)
        replacement = playback.read_bytes()
        if b"addon.infillJoinRepairs" not in replacement or replacement == original_program:
            raise ValueError("The installed playback program must support the spoken join repairs.")
        line = b"Playback.lua\n"
        if original_toc.count(line) != 1:
            raise ValueError("The verified player-name add-on no longer has the expected playback entry.")
        overlays.update({
            MANIFEST: manifest,
            REPAIRS + ".lua": _write_lua(routes),
            PROGRAM: replacement,
            TOC: original_toc.replace(line, b"JoinRepairs.lua\n" + line),
        })
        previous = {PROGRAM: sha256(original_program).hexdigest(), TOC: sha256(original_toc).hexdigest()}
        return overlays, previous
