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


@contextmanager
def release_archive_parts(game, catalog, manifest):
    paths = json.loads(manifest.read_bytes())
    if not isinstance(paths, dict) or not paths or paths.keys() - RELEASE_PACK_ROOTS.keys():
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
        yield ReleaseGameArchive(sources, roots)


'''


def placement_script(source, output, hashes):
    text = source.read_text(encoding="utf-8")

    def replace(before, after):
        nonlocal text
        if text.count(before) != 1:
            raise ValueError("The verified placement recipe changed near " + before.split("\n")[0])
        text = text.replace(before, after, 1)

    replace("class SplitGameArchive:", f"RELEASE_HASHES = {hashes!r}\n"
            f"RELEASE_CORE_ROOTS = {CORE_ROOTS!r}\nRELEASE_PACK_ROOTS = {PACK_ROOTS!r}\n\n\n"
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
    replace("repair_cancel=None, catalog=None):", "repair_cancel=None, catalog=None, packs=None):")
    replace("with checked_archive(archive, checking, catalog=catalog) as (source, entries):",
            "with checked_archive(archive, checking, catalog=catalog, packs=packs) as (source, entries):\n"
            "                selected_roots = source.roots if packs else ROOTS\n"
            "                if packs:\n"
            "                    retired = [p for p in retired if p.split('/', 1)[0] in selected_roots]")
    replace('state = {"existing": [], "committed": False}', 'state = {"existing": [], "committed": False, "roots": selected_roots}')
    replace("finishing(0, len(ROOTS))", "finishing(0, len(selected_roots))")
    replace("for index, name in enumerate(ROOTS, 1):", "for index, name in enumerate(selected_roots, 1):")
    replace("finishing(index, len(ROOTS))", "finishing(index, len(selected_roots))")
    replace('        parser.add_argument("--catalog", type=Path, help=SUPPRESS)',
            '        parser.add_argument("--catalog", type=Path, help=SUPPRESS)\n'
            '        parser.add_argument("--release-packs", type=Path, help=SUPPRESS)')
    replace("catalog=args.catalog.resolve() if args.catalog else None)",
            "catalog=args.catalog.resolve() if args.catalog else None,\n"
            "                    packs=args.release_packs.resolve() if args.release_packs else None)")
    compile(text, "install_addon.py", "exec")
    output.write_text(text, encoding="utf-8", newline="\n")
    return output
