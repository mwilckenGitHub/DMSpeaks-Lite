"""Split the verified game ZIP and adapt its existing transactional installer."""

from shutil import copyfileobj
from zipfile import ZipFile, ZipInfo


COLLECTIONS = ("Alliance", "Horde", "Shared", "Gossip")
CORE = "DMSpeaks-WoW.zip"
PACK_ROOTS = {f"DMSpeaks-Lite-{name}-Audio.zip":
              (f"DMSpeaksSpoken{name}", f"DMSpeaksPersonalized{name}") for name in COLLECTIONS}
CORE_ROOTS = ("DMSpeaksLite", "DMSpeaksNameData", "DMSpeaksSpokenNames")


def split_game(source, product, settings):
    names = {
        **dict.fromkeys(CORE_ROOTS, CORE),
        **{root: pack for pack, roots in PACK_ROOTS.items() for root in roots},
    }
    from contextlib import ExitStack
    with ZipFile(source) as archive, ExitStack() as stack:
        output = {name: stack.enter_context(ZipFile(product / name, "x"))
                  for name in (CORE, *PACK_ROOTS)}
        for info in archive.infolist():
            root, _, _ = info.filename.partition("/")
            destination = output[names[root]]
            item = ZipInfo(info.filename, date_time=info.date_time)
            item.compress_type = info.compress_type
            item.external_attr = info.external_attr
            item.create_system = info.create_system
            item.file_size = info.file_size
            if info.filename == "DMSpeaksLite/Settings.lua":
                destination.writestr(item, settings.read_bytes())
            else:
                with archive.open(info) as incoming, destination.open(item, "w") as outgoing:
                    copyfileobj(incoming, outgoing, length=4 * 1024 * 1024)


PARTS = '''
class ReleaseGameArchive:
    def __init__(self, sources, roots):
        self.sources = sources
        self.roots = roots
        self.entries = {}
        for source, allowed in sources:
            found = set()
            for info in source.infolist():
                root = info.filename.split("/", 1)[0]
                if root not in allowed or info.filename in self.entries:
                    raise ValueError("The Lite archive contains an unexpected add-on file.")
                self.entries[info.filename] = (source, info)
                found.add(root)
            if found != set(allowed):
                raise ValueError("The Lite archive is missing an add-on folder.")

    def infolist(self):
        return [info for _, info in self.entries.values()]

    def open(self, info):
        return self.entries[info.filename][0].open(info)

    def extract(self, info, destination):
        return self.entries[info.filename][0].extract(info, destination)


def reused_media_entries(addons, entries, revision, progress):
    """Update the program-owned overlay without rescanning a completed media installation."""
    if not RELEASE_OVERLAYS or not revision:
        return None
    marker = addons.parent / ".dmspeaks-setup-version.txt"
    if is_link(marker) or not marker.is_file() or marker.stat().st_size > 80:
        return None
    recorded = marker.read_text(encoding="ascii", errors="replace").strip()
    if recorded not in (*RELEASE_MEDIA_REVISIONS, revision):
        return None
    # A completed install is the evidence that individual voice files were
    # placed. Check each selected pack, the indexes, and the catalog before
    # taking the fast path. Repair retains the full per-file scan.
    probes, representatives, overlay = [], {}, []
    for info, relative in entries:
        if relative.as_posix() in RELEASE_OVERLAYS:
            overlay.append((info, relative))
        elif relative.suffix == ".toc" or relative == CATALOG or relative.name == "DialogueIndex.lua":
            probes.append((info, relative))
        elif relative.suffix == ".ogg":
            representatives.setdefault(relative.parts[0], (info, relative))
    probes.extend(representatives.values())
    checked, missing = {addons}, []
    for index, (info, relative) in enumerate(probes, 1):
        progress(index - 1, len(probes))
        path = addons / relative
        ensure_regular_folder(path.parent, addons, checked, missing)
        info_at_path = plain_path(path)
        if (info_at_path is None or not S_ISREG(info_at_path.st_mode)
                or not matches_archive(path, info, info_at_path.st_size)):
            return None
    progress(len(probes), len(probes))
    return overlay


@contextmanager
def release_archive_parts(game, catalog, manifest):
    paths = json.loads(manifest.read_bytes())
    if not isinstance(paths, dict) or paths.keys() - RELEASE_PACK_ROOTS.keys():
        raise ValueError("The selected Lite voice packs are invalid.")
    with ExitStack() as stack:
        def open_part(path, checksum):
            stream = stack.enter_context(path.open("rb"))
            if file_digest(stream, "sha256").hexdigest() != checksum:
                raise ValueError(f"{path.name} does not match this release.")
            stream.seek(0)
            return stack.enter_context(ZipFile(stream))

        prepared = open_part(catalog, CATALOG_PAYLOAD_SHA256)
        if prepared.namelist() != [CATALOG.as_posix()]:
            raise ValueError("The prepared Lite catalog has an unexpected layout.")
        sources = [(game, RELEASE_CORE_ROOTS), (prepared, ("DMSpeaksNameData",))]
        roots = list(RELEASE_CORE_ROOTS)
        for name, path in paths.items():
            if not isinstance(path, str) or not Path(path).is_absolute() or Path(path).name != name:
                raise ValueError("The selected Lite voice pack path is invalid.")
            allowed = RELEASE_PACK_ROOTS[name]
            sources.append((open_part(Path(path), RELEASE_HASHES[name]), allowed))
            roots.extend(allowed)
        release = ReleaseGameArchive(sources, roots)
        if RELEASE_OVERLAYS:
            from io import BytesIO

            settings = "DMSpeaksLite/Settings.lua"
            if settings in RELEASE_OVERLAYS and settings not in release.entries:
                raise ValueError("The verified Lite archive is missing its settings file.")
            replacement = stack.enter_context(ZipFile(BytesIO(), "w"))
            for name, content in RELEASE_OVERLAYS.items():
                replacement.writestr(name, content)
                release.entries[name] = (replacement, replacement.getinfo(name))
        yield release


'''


def placement_script(source, output, hashes, *, settings_overlay=None, game_overlay=None, media_revisions=()):
    text = source.read_text(encoding="utf-8")
    overlays = dict(game_overlay or {})
    if settings_overlay is not None:
        overlays["DMSpeaksLite/Settings.lua"] = settings_overlay

    def replace(before, after):
        nonlocal text
        if text.count(before) != 1:
            raise ValueError("The verified placement recipe changed near " + before.split("\n")[0])
        text = text.replace(before, after, 1)

    replace("class SplitGameArchive:", f"RELEASE_HASHES = {hashes!r}\n"
            f"RELEASE_CORE_ROOTS = {CORE_ROOTS!r}\nRELEASE_PACK_ROOTS = {PACK_ROOTS!r}\n"
            f"RELEASE_OVERLAYS = {overlays!r}\nRELEASE_MEDIA_REVISIONS = {media_revisions!r}\n\n\n"
            + PARTS + "class SplitGameArchive:")
    replace("for name in reversed(ROOTS):", 'for name in reversed(state.get("roots", ROOTS)):')
    replace("def checked_archive(archive, progress=None, *, catalog=None):",
            "def checked_archive(archive, progress=None, *, catalog=None, packs=None):")
    replace("        if archive_sha not in (ARCHIVE_SHA256, COMPACT_ARCHIVE_SHA256):",
            f"        accepted = (RELEASE_HASHES[{CORE!r}],) if packs else (ARCHIVE_SHA256, COMPACT_ARCHIVE_SHA256)\n"
            "        if archive_sha not in accepted:")
    replace("        with ZipFile(stream) as game, archive_parts(game, archive_sha, catalog) as source:",
            "        with ZipFile(stream) as game:\n"
            "            selection = (release_archive_parts(game, catalog, packs) if packs\n"
            "                         else archive_parts(game, archive_sha, catalog))\n"
            "            with selection as source:\n"
            "                yield source, validated_entries(source)\n\n\n"
            "def validated_entries(source):")
    start = text.index("            entries = ", text.index("def validated_entries"))
    end = text.index("\n\n\ndef placement_worker_count", start)
    body = "\n".join(line[8:] for line in text[start:end].split("\n"))
    body = ('    expected = source.roots if isinstance(source, ReleaseGameArchive) else ROOTS\n'
            + body.replace("set(ROOTS)", "set(expected)").replace("for name in ROOTS", "for name in expected")
            .replace("yield source, entries", "return entries"))
    text = text[:start] + body + text[end:]
    replace("repair_cancel=None, catalog=None):",
            "repair_cancel=None, catalog=None, packs=None, setup_revision=None):")
    replace('            checking = phase("Checking the voice package", start, 40)\n'
            '            if repair:\n                checking = repair_progress(checking)\n'
            '            else:\n                checking(0, 1)',
            '            checking = repair_progress(phase("Checking the voice package", start, 40))')
    replace('                        progress = phase("Checking installed voice files", 40, 92, files=True)',
            '                        progress = repair_progress(phase("Checking installed voice files", 40, 92, files=True))')
    replace('                else:\n                    if repair_cancel is not None:\n'
            '                        repair_cancel(before_apply=True)\n                    extract_archive(source, entries, incoming, progress)',
            '                else:\n                    extract_archive(source, entries, incoming, repair_progress(progress))\n'
            '                    if repair_cancel is not None:\n                        repair_cancel(before_apply=True)')
    replace('        if args.repair_cancel_window and not args.repair:\n'
            '            parser.error("Repair cancellation requires --repair.")\n', '')
    replace('print("Repair canceled. No staged files were applied.", flush=True)',
            'print("Canceled. No staged WoW files were applied.", flush=True)')
    replace("with checked_archive(archive, checking, catalog=catalog) as (source, entries):",
            "with checked_archive(archive, checking, catalog=catalog, packs=packs) as (source, entries):\n"
            "                selected_roots = source.roots if packs else ROOTS\n"
            "                if packs:\n"
            "                    retired = [p for p in retired if p.split('/', 1)[0] in selected_roots]\n"
            "                if packs and existing and not repair and not retired:\n"
            '                    checking_media = repair_progress(phase("Checking installed packs", 40, 88))\n'
            "                    reused = reused_media_entries(addons, entries, setup_revision, checking_media)\n"
            "                    if reused is not None:\n"
            '                        updating = repair_progress(phase("Updating the WoW add-on", 88, 92))\n'
            "                        state = stage_update(addons, work, source, reused, group_folders=True, progress=updating)\n"
            "                        if repair_cancel is not None:\n"
            "                            repair_cancel(before_apply=True)\n"
            "                        apply_update(addons, work, marker, state, finishing)\n"
            '                        print("Kept the installed voice packs.", flush=True)\n'
            "                        return")
    replace('state = {"existing": [], "committed": False}', 'state = {"existing": [], "committed": False, "roots": selected_roots}')
    replace("finishing(0, len(ROOTS))", "finishing(0, len(selected_roots))")
    replace("for index, name in enumerate(ROOTS, 1):", "for index, name in enumerate(selected_roots, 1):")
    replace("finishing(index, len(ROOTS))", "finishing(index, len(selected_roots))")
    replace('        parser.add_argument("--catalog", type=Path, help=SUPPRESS)',
            '        parser.add_argument("--catalog", type=Path, help=SUPPRESS)\n'
            '        parser.add_argument("--setup-revision", help=SUPPRESS)\n'
            '        parser.add_argument("--release-packs", type=Path, help=SUPPRESS)')
    replace("catalog=args.catalog.resolve() if args.catalog else None)",
            "catalog=args.catalog.resolve() if args.catalog else None,\n"
            "                    packs=args.release_packs.resolve() if args.release_packs else None,\n"
            "                    setup_revision=args.setup_revision)")
    compile(text, "install_addon.py", "exec")
    output.write_text(text, encoding="utf-8", newline="\n")
    return output
