"""Retire verified DMSpeaks runtime files while retaining other games' installations."""
import argparse
from collections import defaultdict
from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes as wt
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from tempfile import NamedTemporaryFile
import uuid
import winreg


INVENTORY_SHA256 = "87eeef25fee313a57b78eaafa1faa738508b7e46a25bdf58e91f6d0285f2235f"
UPDATE_LOCK = ".beta-update.lock"
REGISTRATION = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
shell = ctypes.WinDLL("shell32")
ole = ctypes.OleDLL("ole32")
INVALID = ctypes.c_void_p(-1).value
H, D, P = wt.HANDLE, wt.DWORD, ctypes.c_void_p


def signature(lib, name, result, *args):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = result, args
    return fn


open_file = signature(kernel, "CreateFileW", H, wt.LPCWSTR, D, D, P, D, D, H)
close = signature(kernel, "CloseHandle", wt.BOOL, H)
read = signature(kernel, "ReadFile", wt.BOOL, H, P, D, ctypes.POINTER(D), P)
set_info = signature(kernel, "SetFileInformationByHandle", wt.BOOL, H, ctypes.c_int, P, D)
get_size = signature(kernel, "GetFileSizeEx", wt.BOOL, H, ctypes.POINTER(ctypes.c_longlong))
create_mutex = signature(kernel, "CreateMutexW", H, P, wt.BOOL, wt.LPCWSTR)
wait = signature(kernel, "WaitForSingleObject", D, H, D)
release = signature(kernel, "ReleaseMutex", wt.BOOL, H)


def checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def key(path):
    return os.path.normcase(os.path.normpath(str(path)))


def ordinary(path):
    """Return False for absent paths. A reparse point can never authorize cleanup."""
    try:
        attrs = path.lstat().st_file_attributes
    except FileNotFoundError:
        return False
    if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"Maintenance path is redirected: {path}")
    return True


def trusted_path(path):
    if not path.is_absolute() or ".." in path.parts or path.drive.startswith("\\\\"):
        raise ValueError(f"An absolute local path is required: {path}")
    for part in (*reversed(path.parents), path):
        ordinary(part)
    return path


@contextmanager
def serialize(builds):
    name = "Local\\DMSpeaks-runtime-maintenance-" + hashlib.sha256(key(builds).encode()).hexdigest()
    handle = checked(create_mutex(None, False, name))
    try:
        if wait(handle, 0) not in (0, 0x80):
            raise OSError("Another runtime maintenance operation is active. Retry when it finishes.")
        try:
            yield
        finally:
            checked(release(handle))
    finally:
        close(handle)


def known_folder(number):
    buffer = ctypes.create_unicode_buffer(32768)
    result = shell.SHGetFolderPathW(None, number | 0x4000, None, 0, buffer)
    if result != 0:
        raise OSError(f"Could not identify Windows shell folder {number}.")
    return Path(buffer.value)


def com_method(pointer, index, *types):
    address = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(P))).contents[index]
    return ctypes.WINFUNCTYPE(ctypes.c_long, P, *types)(address)


def shortcut(path):
    """Read a Shell Link without resolving or launching its target."""
    def guid(text):
        return ctypes.create_string_buffer(uuid.UUID(text).bytes_le)
    link, persist = P(), P()
    clsid = guid("00021401-0000-0000-c000-000000000046")
    iface = guid("000214f9-0000-0000-c000-000000000046")
    file_iface = guid("0000010b-0000-0000-c000-000000000046")
    ole.CoInitialize(None)
    try:
        ole.CoCreateInstance(clsid, None, 1, iface, ctypes.byref(link))
        if com_method(link, 0, P, P)(link, file_iface, ctypes.byref(persist)) < 0:
            raise OSError("Shell link lacks its storage interface.")
        if com_method(persist, 5, wt.LPCWSTR, D)(persist, str(path), 0) < 0:
            raise OSError(f"Could not read shortcut {path}")
        target, arguments = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(32768)
        if (com_method(link, 3, P, ctypes.c_int, P, D)(link, target, len(target), None, 4) < 0 or
                com_method(link, 10, P, ctypes.c_int)(link, arguments, len(arguments)) < 0):
            raise OSError(f"Could not read shortcut target {path}")
        return Path(os.path.expandvars(target.value)), arguments.value
    finally:
        for obj in (persist, link):
            if obj.value:
                com_method(obj, 2)(obj)
        ole.CoUninitialize()


def windows_args(command):
    argc = ctypes.c_int()
    parse = signature(shell, "CommandLineToArgvW", ctypes.POINTER(wt.LPWSTR), wt.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    argv = checked(parse("arg0 " + command, ctypes.byref(argc)))
    try:
        return list(argv[:argc.value])[1:]
    finally:
        signature(kernel, "LocalFree", H, P)(argv)


def installed_games(product):
    """Registry keys with this install location supply game paths, not build identities."""
    paths = set()
    token = H()
    buffer = ctypes.create_unicode_buffer(32768)
    size = D(len(buffer))
    token_open = signature(ctypes.WinDLL("advapi32"), "OpenProcessToken", wt.BOOL, H, D, ctypes.POINTER(H))
    profile = signature(ctypes.WinDLL("userenv"), "GetUserProfileDirectoryW", wt.BOOL, H, P, ctypes.POINTER(D))
    checked(token_open(H(-1), 8, ctypes.byref(token)))
    try:
        checked(profile(token, buffer, ctypes.byref(size)))
    finally:
        close(token)
    real = key(Path(buffer.value)) == key(Path(os.environ["USERPROFILE"])) and key(product.parent) == key(known_folder(28))
    if real:
        try:
            registry = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRATION, access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        except FileNotFoundError:
            return real, paths
        with registry as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                name = winreg.EnumKey(root, i)
                if not name.casefold().startswith("dmspeaks"):
                    continue
                with winreg.OpenKey(root, name) as app:
                    try:
                        location = Path(winreg.QueryValueEx(app, "InstallLocation")[0])
                    except FileNotFoundError:
                        continue
                    if key(location) == key(product):
                        paths.add(key(Path(winreg.QueryValueEx(app, "GameFolder")[0])))
    return real, paths


def shortcut_clients(builds, real):
    product = builds.parent
    if real:
        programs, desktop = known_folder(2), known_folder(16)
    else:
        programs = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
        desktop = Path(os.environ["USERPROFILE"]) / "Desktop"
    refs = defaultdict(set)
    for folder, recursive in ((product, False), (programs, True), (desktop, False)):
        if not folder.is_dir():
            continue
        for lnk in folder.rglob("*.lnk") if recursive else folder.glob("*.lnk"):
            if not ordinary(lnk):
                continue
            trusted_path(lnk)
            try:
                target, args = shortcut(lnk)
            except OSError:
                if "dmspeaks" in lnk.name.casefold():
                    raise
                continue
            if not target.is_relative_to(builds):
                continue
            rel = target.relative_to(builds)
            if len(rel.parts) != 4 or tuple(p.lower() for p in rel.parts[1:]) != ("runtime","python","pythonw.exe"):
                raise ValueError(f"Unrecognized application target in {lnk}")
            parsed = windows_args(args)
            if not parsed or not Path(parsed[-1]).is_absolute():
                raise ValueError(f"Cannot identify the game for {lnk}")
            launchers = {key(product/"Maintenance/open_lite.py"), key(builds/rel.parts[0]/"source/wow_fetch_desktop.py")}
            if not any(key(Path(arg)) in launchers for arg in parsed[:-1]):
                raise ValueError(f"Cannot identify the DMSpeaks launcher for {lnk}")
            game = trusted_path(Path(parsed[-1]))
            refs[key(game)].add(rel.parts[0])
    return refs


def atomic_write(path, data):
    trusted_path(path)
    with NamedTemporaryFile(dir=path.parent, delete=False) as out:
        staged = Path(out.name)
        try:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            out.close()
            staged.unlink(missing_ok=True)
            raise
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def reference_file(folder, game):
    return folder / (hashlib.sha256(key(game).encode()).hexdigest()+".json")


def read_claim(path):
    ordinary(path)
    record = json.loads(path.read_bytes())
    if not isinstance(record, dict) or set(record) != {"game", "build"} or not re.fullmatch("[0-9a-f]{20}", record["build"]):
        raise ValueError(f"Cannot verify runtime client record {path}")
    game = trusted_path(Path(record["game"]))
    if reference_file(path.parent, game) != path:
        raise ValueError(f"Cannot identify the game in {path}")
    return key(game), record["build"]


def references(folder):
    result = {}
    if not ordinary(folder):
        return result
    for path in folder.glob("*.json"):
        if re.fullmatch("[0-9a-f]{64}", path.stem):
            game, build = read_claim(path)
            result[game] = build
    return result


def remove_claim(folder, game, expected_build):
    path = reference_file(folder, game)
    if read_claim(path) != (game, expected_build):
        raise ValueError("The game reference changed during maintenance. Retry.")
    path.unlink()


def write_claim(folder, game, build):
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write(reference_file(folder, game), json.dumps({"game": str(Path(game).resolve()), "build": build}).encode())


def remove_empty(folder):
    if ordinary(folder) and not any(folder.iterdir()):
        folder.rmdir()


def process_images():
    enum = signature(ctypes.WinDLL("psapi"), "EnumProcesses", wt.BOOL, P, D, ctypes.POINTER(D))
    capacity = 1024
    used = D()
    while True:
        ids = (D*capacity)()
        checked(enum(ids, ctypes.sizeof(ids), ctypes.byref(used)))
        if used.value < ctypes.sizeof(ids):
            break
        capacity *= 2
    open_process = signature(kernel, "OpenProcess", H, D, wt.BOOL, D)
    query = signature(kernel, "QueryFullProcessImageNameW", wt.BOOL, H, D, P, ctypes.POINTER(D))
    for pid in ids[:used.value//ctypes.sizeof(D)]:
        handle = open_process(0x1000, False, pid)
        if handle:
            try:
                size = D(32768)
                path = ctypes.create_unicode_buffer(size.value)
                if query(handle, 0, path, ctypes.byref(size)):
                    yield Path(path.value)
            finally:
                close(handle)


def build_files(inventory, build):
    return {**{name: [pin] for name, pin in inventory["common"].items()}, **inventory["builds"][build]}


def verify_peer(builds, build, game, inventory):
    if build not in inventory["builds"] or not trusted_path(Path(game)/"Interface/AddOns").is_dir():
        raise ValueError("A remaining DMSpeaks client cannot be verified.")
    entries = build_files(inventory, build)
    for item in ("runtime/python/pythonw.exe","source/wow_fetch_desktop.py"):
        path = trusted_path(builds/build/item)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if [path.stat().st_size, digest] not in entries[item]:
            raise ValueError(f"Remaining client needs a verified runtime: {path}")


def open_locked(path, directory=False):
    flags = 0x00200000 | (0x02000000 if directory else 0)
    # Lock directory names against renaming; lock file bytes against writes and replacement.
    handle = open_file(str(path), 0x80 if directory else 0x80010000,
                       3 if directory else 1, None, 3, flags, None)
    if handle == INVALID or not handle:
        raise ctypes.WinError(ctypes.get_last_error(), f"Cannot safely lock {path}")
    return handle


def prune(build_root, approved):
    if not ordinary(build_root):
        return 0
    directory_handles = set()
    removals = []
    buffer = ctypes.create_string_buffer(1024 * 1024)
    with ExitStack() as stack:
        def pin_directory(directory):
            if directory in directory_handles:
                return
            if directory != build_root:
                pin_directory(directory.parent)
            handle = open_locked(directory, True)
            stack.callback(close, handle)
            ordinary(directory)
            directory_handles.add(directory)
        pin_directory(build_root)
        for rel, choices in approved.items():
            path = build_root / rel
            # Check parents before even inspecting a redirected leaf.
            try:
                pin_directory(path.parent)
            except FileNotFoundError:
                continue
            if not ordinary(path) or path.is_dir():
                continue
            lengths = {size for size, _ in choices}
            # The updater can also lock a pre-existing, customized sentinel.
            # Exclude its writer before pruning any files in this build.
            if rel != UPDATE_LOCK and path.stat().st_size not in lengths:
                continue
            handle = open_locked(path)
            stack.callback(close, handle)
            ordinary(path)
            size = ctypes.c_longlong()
            checked(get_size(handle, ctypes.byref(size)))
            if size.value not in lengths:
                continue
            digest, size_read = hashlib.sha256(), D()
            while True:
                checked(read(handle, buffer, len(buffer), ctypes.byref(size_read), None))
                if not size_read.value:
                    break
                digest.update(memoryview(buffer)[:size_read.value])
            if [size.value, digest.hexdigest()] in choices:
                if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
                    raise PermissionError(f"An installed file is read-only: {path}")
                removals.append(handle)
        # Preflight the entire build before the first file is marked for deletion.
        for handle in removals:
            disposition = wt.BOOL(True)
            checked(set_info(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)))
    for directory in sorted(directory_handles, key=lambda p:len(p.parts), reverse=True):
        if not ordinary(directory):
            continue
        try:
            directory.rmdir()
        except OSError as exc:
            if exc.winerror != 145:  # ENOTEMPTY: keep unknown and modified content.
                raise
    return len(removals)


def run(args):
    packed = Path(__file__).with_name("runtime-files.json.gz").read_bytes()
    if hashlib.sha256(packed).hexdigest() != INVENTORY_SHA256:
        raise ValueError("Runtime ownership inventory verification failed.")
    inventory = json.loads(gzip.decompress(packed))
    builds, game = trusted_path(args.builds), trusted_path(args.game)
    product = builds.parent
    if (key(builds) != key(Path(os.environ["LOCALAPPDATA"])/"DMSpeaksLite/Builds")
            or args.build not in inventory["builds"] or Path(sys.executable).is_relative_to(builds)):
        raise ValueError("Maintenance must run outside the approved runtime directory.")
    claim_root = trusted_path(product/"RuntimeClients")
    legacy_root = trusted_path(product/"Maintenance/RuntimeClients")
    current = key(game)
    if args.result:
        result = trusted_path(args.result)
        if any(result.is_relative_to(p) for p in (product, game)) or not result.parent.is_dir():
            raise ValueError("Use a separate existing private directory for the maintenance result.")
    with serialize(builds), ExitStack() as locked:
        for folder in (product, builds):
            if ordinary(folder):
                locked.callback(close, open_locked(folder, True))
        canonical = references(claim_root)
        legacy = references(legacy_root)
        if any(canonical[client] != legacy[client] for client in canonical.keys() & legacy.keys()):
            raise ValueError("Conflicting runtime client records need attention before cleanup.")
        claims = {**legacy, **canonical}
        real, registered = installed_games(product)
        links = shortcut_clients(builds, real)
        others = {}
        for client in sorted((claims.keys() | links.keys() | registered) - {current}):
            ids = set(links.get(client, ()))
            if client in claims:
                ids.add(claims[client])
            if len(ids) != 1:
                raise ValueError("Cannot identify the runtime of another registered game.")
            build = ids.pop()
            verify_peer(builds, build, client, inventory)
            others[client] = build
        if args.mode != "inspect":
            keep = set(others.values())
            if args.mode == "upgrade":
                verify_peer(builds, args.build, current, inventory)
                keep.add(args.build)
            candidates = {}
            for name in inventory["builds"]:
                if not ordinary(builds/name):
                    continue
                if name not in keep:
                    # placement_lock() opens this path with a+b and writes no payload.
                    candidates[name] = {UPDATE_LOCK: [[0, hashlib.sha256(b"").hexdigest()]],
                                        **build_files(inventory,name),
                                        **{k:[v] for k,v in inventory["media"].items()}}
                elif args.mode == "upgrade" and name == args.build and name not in others.values():
                    retired = {k:[v] for k,v in inventory["media"].items() if k != inventory["active_media"]}
                    if retired:
                        candidates[name] = retired
            active = {p.relative_to(builds).parts[0] for p in process_images() if p.is_relative_to(builds) and p != builds}
            if active & candidates.keys():
                raise OSError("A selected runtime is still in use. Close its application and retry.")
            for name, entries in candidates.items():
                prune(builds/name, entries)
            # First commit the original records, then retire their old copies.
            # An interruption can leave only identical duplicates. Update this
            # client's build only after the migration is complete.
            for client, build in legacy.items():
                if client not in canonical:
                    write_claim(claim_root, client, build)
                remove_claim(legacy_root, client, build)
            remove_empty(legacy_root)
            for client, build in {**others, **({current:args.build} if args.mode == "upgrade" else {})}.items():
                if claims.get(client) != build:
                    write_claim(claim_root, client, build)
            if args.mode == "uninstall" and current in claims:
                remove_claim(claim_root, current, claims[current])
            remove_empty(claim_root)
        if args.result:
            lines = ["[clients]", "shared="+str(int(bool(others)))]
            if others:
                peer = min(others)
                lines.extend(["game="+str(Path(peer).resolve()), "build="+others[peer]])
            atomic_write(result, ("\r\n".join(lines)+"\r\n").encode("utf-16"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("upgrade","uninstall","inspect"))
    parser.add_argument("--builds", required=True, type=Path)
    parser.add_argument("--build", required=True)
    parser.add_argument("--game", required=True, type=Path)
    parser.add_argument("--result", type=Path)
    try:
        run(parser.parse_args())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Runtime maintenance could not finish: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
