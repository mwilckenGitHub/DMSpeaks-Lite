"""Start the shared WoW listener and open its Fetch Settings when requested."""

import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import errno
from hashlib import sha256
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import subprocess
import sys
import traceback
from tempfile import NamedTemporaryFile
from time import monotonic, sleep
from urllib.parse import urlencode
import webbrowser
from xml.etree.ElementTree import Element, SubElement, tostring

# The sign-in launch uses isolated Python. Load sibling modules from this package.
if __name__ == "__main__" and sys.flags.isolated:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from launch import check_deps, ensure_config


LISTENER_PROTOCOL = 4
DESKTOP_VERSION = "0.1.0-beta.12.8"
WINDOWS_LISTENER_STARTUP_TIMEOUT = 75
DEFAULT_PORT = 8770
INSTALLER_PORT_COUNT = 8
BUILD_DIRECTORIES = ("formats", "lite_addon", "lite_web", "third_party")
DIFFERENT_LISTENER = {"outdated", "different-catalog", "different-build", "different-data"}


def packaged_plans(package):
    archive = package / "Prepared-dialogue-plans.zip"
    pin = package / "Prepared-dialogue-plans.sha256"
    if not any(path.exists() or path.is_symlink() for path in (archive, pin)):
        return None, None
    if (any(path.is_symlink() or not path.is_file() for path in (archive, pin))
            or pin.stat().st_size > 128):
        raise ValueError("The optional dialogue catalog needs both its ZIP and its hash receipt.")
    checksum = pin.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("The optional dialogue catalog needs a SHA-256 hash.")
    return archive, checksum


def packaged_paced(package):
    """Read companion-owned preparation assets without machine-specific paths."""
    pin=package/'Prepared-paced-deliveries.json'
    archive=package/'Prepared-paced-deliveries.zip'
    if not pin.exists() and not archive.exists():
        return None
    if (pin.is_symlink() or archive.is_symlink() or not pin.is_file() or not archive.is_file()
            or pin.stat().st_size>4096):
        raise ValueError('Extract the complete prepared paced collection and its hash receipt.')
    data=json.loads(pin.read_text(encoding='utf-8'))
    if (not isinstance(data,dict) or set(data)!={'sha256','input_sha256'}
            or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in data.values())):
        raise ValueError('The prepared paced collection needs its archive and input hashes.')
    return dict(path=str(archive),**data)


PREPARED_INPUTS_NAME = "DMSpeaks-Prepared-Inputs.json"

def store_inputs(root):
    """Read the Name catalog receipt from the installed CurseForge add-on."""
    addons = (Path(root).resolve() / "Interface" / "AddOns").resolve()
    module = addons / "DMSpeaksLite"
    if module.is_symlink() or not (module / "DMSpeaksLite.toc").is_file():
        raise ValueError("Install DMSpeaks for WoW in CurseForge, then run Lite setup again.")
    # CurseForge installs the regional name data as a required project.
    # Smaller rehearsal builds can carry the same catalog in their UI addon.
    name_data = addons / "DMSpeaksNameData"
    for catalog in (name_data / "NameFetch", module / "NameFetch"):
        if catalog.resolve() != catalog:
            raise ValueError("Reinstall the DMSpeaks add-on in CurseForge to repair its Name catalog.")
        paths = packaged_plans(catalog)
        if paths[0] is not None:
            return paths
    return None, None


def prepared_inputs(package, receipt=None):
    """Find optional adjacent preview inputs without copying them into the app."""
    if receipt is None:
        plans, pin = packaged_plans(package)
        return plans, pin, packaged_paced(package)

    receipt = Path(receipt)
    if (not receipt.is_absolute() or receipt.name != PREPARED_INPUTS_NAME or receipt.is_symlink()
            or not receipt.is_file() or receipt.stat().st_size > 4096):
        raise ValueError("DMSpeaks could not read the preview's prepared-input file.")
    root = receipt.parent.resolve()
    data = json.loads(receipt.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"format", "catalog", "playback"} or (
            data["format"] != "dmspeaks-prepared-inputs-1"):
        raise ValueError("DMSpeaks could not recognize the preview's prepared-input file.")

    def archive(row, fields):
        if (not isinstance(row, dict) or set(row) != {"file", *fields}
                or any(not isinstance(row[key], str) or not re.fullmatch(r"[0-9a-f]{64}", row[key])
                       for key in fields)):
            raise ValueError("A prepared dialogue file is missing its verification codes.")
        relative = row["file"]
        if (not isinstance(relative, str) or len(relative) > 240 or not relative.endswith(".zip")
                or any(char in relative for char in "\\:*?[]")
                or any(part in ("", ".", "..") for part in relative.split("/"))):
            raise ValueError("Prepared dialogue files must be inside the preview folder.")
        file = root / relative
        if file.resolve() != file or not file.is_file():
            raise ValueError(f"The preview is missing {file.name}. Restore it beside the installer.")
        return file

    plans = archive(data["catalog"], {"sha256"})
    playback = archive(data["playback"], {"sha256", "input_sha256"})
    if plans == playback:
        raise ValueError("Choose separate catalog and playback archives.")
    return plans, data["catalog"]["sha256"], {
        "path": str(playback), "sha256": data["playback"]["sha256"],
        "input_sha256": data["playback"]["input_sha256"],
    }


def client_id(root):
    return sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()


def companion_build_id():
    """Identify the shared code and assets independently of the Windows runtime."""
    root = Path(__file__).resolve().parent
    files = list(root.glob("*.py"))
    for name in BUILD_DIRECTORIES:
        files.extend(path for path in (root / name).rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts and path.suffix != ".pyc")
    build = sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        build.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        build.update(sha256(path.read_bytes()).digest())
    for name in ("Prepared-dialogue-plans.sha256", "Prepared-paced-deliveries.json"):
        pin = root.parent / name
        if pin.is_file():
            build.update(name.encode("ascii") + b"\0" + sha256(pin.read_bytes()).digest())
    return build.hexdigest()


def listener_state(port, root, plans_sha256=None, build_id=None, *, folder=None):
    if sys.platform == "win32":
        # Windows can take two seconds to refuse a connection to an unused
        # port. An exclusive bind distinguishes it from a silent listener
        # immediately, including when scanning the installer port range.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                pass
            else:
                return "absent"
    connection = HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        query = {"client": client_id(root)}
        if folder is not None:
            query["data"] = client_id(folder)
        connection.request("GET", "/api/wow-fetch-ready?" + urlencode(query))
        response = connection.getresponse()
        if response.status != 200:
            return "other"
        payload = json.loads(response.read(256))
        if not isinstance(payload, dict) or payload.get("ready") is not True:
            return "other"
        if payload.get("protocol") != LISTENER_PROTOCOL:
            return "outdated"
        if plans_sha256 is not None and payload.get("catalog") != plans_sha256:
            return "different-catalog"
        if build_id is not None and payload.get("build") != build_id:
            return "different-build"
        if folder is not None and payload.get("data_matches") is not True:
            return "different-data"
        return "ready"
    except ConnectionRefusedError:
        # A Windows port may be reserved or bound without listening.
        return "other" if sys.platform == "win32" else "absent"
    except (OSError, ValueError, HTTPException):
        return "other"
    finally:
        connection.close()


def installer_listener(preferred_port, root, catalog, build, folder):
    """Prefer the existing listener for this client, then the first free port."""
    available_port = None
    for port in range(preferred_port, min(preferred_port + INSTALLER_PORT_COUNT, 65536)):
        state = listener_state(port, root, catalog, build, folder=folder)
        if state == "ready" or state in DIFFERENT_LISTENER:
            return port, state
        if state == "absent" and available_port is None:
            available_port = port
    if available_port is None:
        raise ValueError("DMSpeaks could not find a free local port for Fetch Settings.")
    return available_port, "absent"


def windows_runtime_listener_ports():
    """Find IPv4 loopback listeners owned by this installed Python runtime."""
    import ctypes
    from ctypes import wintypes
    from struct import Struct

    table = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    table.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                      wintypes.BOOL, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
    table.restype = wintypes.DWORD
    size = wintypes.DWORD()
    buffer = None
    for _ in range(3):
        code = table(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if code == 0 and buffer is not None:
            break
        if code != 122 or not 4 <= size.value <= 16 * 1024**2:
            return ()
        buffer = ctypes.create_string_buffer(size.value)
    else:
        return ()

    word, row = Struct("<I"), Struct("<6I")
    count, = word.unpack_from(buffer)
    if word.size + count * row.size > len(buffer):
        return ()
    loopback = int.from_bytes(socket.inet_aton("127.0.0.1"), "little")
    ports_by_pid = {}
    for state, address, raw_port, _, _, pid in row.iter_unpack(
            buffer.raw[word.size:word.size + count * row.size]):
        if state == 2 and address == loopback and pid and raw_port:
            ports_by_pid.setdefault(pid, set()).add(socket.ntohs(raw_port & 0xffff))

    kernel = ctypes.WinDLL("kernel32")
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    runtime = Path(sys.executable).resolve().parent
    executable_paths = {runtime / "python.exe", runtime / "pythonw.exe"}
    matches = set()
    for pid, ports in ports_by_pid.items():
        process = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            continue
        try:
            image = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(image))
            if kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(length)):
                if Path(image.value) in executable_paths:
                    matches.update(ports)
        finally:
            kernel.CloseHandle(process)
    return tuple(sorted(matches))


def automatic_listener(root, catalog, build, folder, cleanup):
    """Find the running companion or acquire the right to launch one."""
    from lite_name_bridge import ADDON, status_addon_files
    from project_recovery import WorkingCopyLease

    addons = Path(root).resolve() / "Interface" / "AddOns"
    snapshot, _ = status_addon_files(addons)
    status_files = (snapshot, addons / ADDON / "Status.lua")
    deadline = monotonic() + 12
    while True:
        mismatch = None
        for status_file in status_files:
            if status_file.is_symlink():
                continue
            try:
                # Read only the numeric field written by Lite. The live HTTP
                # check must confirm the WoW folder, catalog, and companion.
                with status_file.open("rb") as status:
                    field = re.search(rb"^    settingsPort = ([0-9]{1,5}),\r?$", status.read(8192), re.M)
            except OSError:
                continue
            if field and 1 <= (port := int(field[1])) <= 65535:
                state = listener_state(port, root, catalog, build, folder=folder)
                if state == "ready":
                    return port, state
                if state in DIFFERENT_LISTENER:
                    mismatch = (port, state)
        if sys.platform == "win32":
            # Desktop fetches and older builds can leave a snapshot without a port.
            # The same installed runtime may still be serving this exact catalog and profile.
            for port in windows_runtime_listener_ports():
                state = listener_state(port, root, catalog, folder=folder)
                if state == "ready":
                    return port, state
                if state in DIFFERENT_LISTENER:
                    mismatch = (port, state)
        if mismatch:
            return mismatch
        try:
            lease = WorkingCopyLease(addons, ".dmspeaks-automatic-launch.lock")
        except OSError as error:
            busy = isinstance(error, BlockingIOError) or (
                os.name == "nt" and error.errno in {errno.EACCES, errno.EDEADLK})
            if not busy:
                raise
            if monotonic() >= deadline:
                raise ValueError("DMSpeaks Lite is still starting. Open it again in a moment.") from None
            sleep(0.2)
        else:
            cleanup.callback(lease.close)
            return 0, "absent"


def open_settings(port, *, provider=None):
    params = {"dmspeaks-fetch": "desktop"}
    if provider:
        params["provider"] = provider
    url = f"http://127.0.0.1:{port}/?{urlencode(params)}"
    if not webbrowser.open(url, new=2):
        raise ValueError(f"Open the Fetch Settings page in your browser at {url}")


def launch_arguments(script, root, port, folder, prepared_receipt=None):
    arguments = ["-I", "-B", str(script.resolve()), str(root.resolve()),
                 "--port", str(port), "--data", str(folder.resolve())]
    if prepared_receipt is not None:
        arguments.extend(("--prepared-inputs", str(prepared_receipt.resolve())))
    return arguments


def xdg_home(variable, fallback):
    configured = Path(os.environ.get(variable, ""))
    return configured if configured.is_absolute() else Path.home() / fallback


def default_data_folder():
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "DMSpeaksLiteBeta"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/DMSpeaks Lite"
    return xdg_home("XDG_DATA_HOME", ".local/share") / "dmspeaks-lite"


def desktop_integration_directory():
    # Keep setup in step with the full editor's WorkspaceLayout::UserDirectory.
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "DMSpeaks/integrations"
    if sys.platform == "darwin":
        return Path.home() / "Library/Preferences/DMSpeaks/integrations"
    return xdg_home("XDG_CONFIG_HOME", ".config") / "dmspeaks/integrations"


def desktop_entry_value(value):
    return value.translate(str.maketrans({"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}))


def desktop_exec_argument(value):
    escaped = value.translate(str.maketrans({char: "\\" + char for char in '\\"$`'}))
    return '"' + escaped.replace("%", "%%") + '"'


def register_posix_background(command, directory):
    from lite_portable_io import cache_bytes

    if sys.platform == "darwin":
        label = "com.dmspeaks.lite"
        startup = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        content = plistlib.dumps({"Label": label, "ProgramArguments": command,
                                 "RunAtLoad": True, "WorkingDirectory": str(directory)})
    elif sys.platform.startswith("linux"):
        startup = xdg_home("XDG_CONFIG_HOME", ".config") / "autostart/dmspeaks-lite.desktop"
        execute = " ".join(desktop_exec_argument(arg) for arg in command)
        content = ("[Desktop Entry]\nType=Application\nName=DMSpeaks Lite\nTerminal=false\n"
                   f"Exec={desktop_entry_value(execute)}\n"
                   f"Path={desktop_entry_value(str(directory))}\n").encode("utf-8")
    else:
        raise ValueError(f"DMSpeaks Lite automatic sign-in is unavailable on {sys.platform}.")
    cache_bytes(startup, content)


def register_integration(directory, launcher, script, root, port, folder, *, prepared_receipt=None):
    """Supply the integration label and arguments to the generic desktop menu."""
    if not (root / "Interface/AddOns/DMSpeaksLite/DMSpeaksLite.toc").is_file():
        raise ValueError("Install the DMSpeaks add-on in the selected WoW client first.")
    if not launcher.is_file() or not script.is_file():
        raise ValueError("The installed DMSpeaks companion is incomplete.")
    document = Element("integration", version="1")
    SubElement(document, "label").text = "DMSpeaks Lite - WOW Fetch Settings"
    executable = launcher.resolve() if sys.platform == "win32" else launcher.absolute()
    SubElement(document, "executable").text = str(executable)
    for arg in launch_arguments(script, root, port, folder, prepared_receipt):
        SubElement(document, "argument").text = arg
    directory.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(tostring(document, encoding="utf-8", xml_declaration=True))
            stream.close()
            temporary.replace(directory / "dmspeaks-for-wow.xml")
        finally:
            temporary.unlink(missing_ok=True)


def register_windows_background(launcher, arguments, directory):
    """Write and replace a Unicode shortcut in the current user's Startup folder."""
    import ctypes as win
    from uuid import UUID, uuid4

    Guid = win.c_ubyte * 16
    Ptr = win.c_void_p
    HResult = win.c_long
    DWord = win.c_uint32
    String = win.c_wchar_p

    def guid(text):
        return Guid.from_buffer_copy(UUID(text).bytes_le)

    def checked(operation, status):
        if status < 0:
            raise OSError(f"{operation} failed (0x{status & 0xffffffff:08x}).")

    def call(interface, slot, result, types, *values):
        table = win.cast(interface, win.POINTER(win.POINTER(Ptr))).contents
        method = win.WINFUNCTYPE(result, Ptr, *types)(table[slot])
        return method(interface, *values)

    shell_link_class = guid("00021401-0000-0000-c000-000000000046")
    shell_link_interface = guid("000214f9-0000-0000-c000-000000000046")
    persist_file_interface = guid("0000010b-0000-0000-c000-000000000046")
    startup_folder_id = guid("b97d20bb-f46a-4c97-ba10-5e3608430854")
    ole = win.WinDLL("ole32")
    shell = win.WinDLL("shell32")
    ole.CoInitializeEx.argtypes = [Ptr, DWord]
    ole.CoInitializeEx.restype = HResult
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [win.POINTER(Guid), Ptr, DWord, win.POINTER(Guid), win.POINTER(Ptr)]
    ole.CoCreateInstance.restype = HResult
    ole.CoTaskMemFree.argtypes = [Ptr]
    ole.CoTaskMemFree.restype = None
    shell.SHGetKnownFolderPath.argtypes = [win.POINTER(Guid), DWord, Ptr, win.POINTER(Ptr)]
    shell.SHGetKnownFolderPath.restype = HResult

    checked("Initialize Windows Shell", ole.CoInitializeEx(None, 2))
    link, persisted, folder = Ptr(), Ptr(), Ptr()
    staged = None
    try:
        # KF_FLAG_CREATE asks the Shell to create the Startup folder if missing.
        checked("Find Windows Startup folder", shell.SHGetKnownFolderPath(
            win.byref(startup_folder_id), 0x8000, None, win.byref(folder)))
        startup = Path(win.wstring_at(folder))
        staged = startup / f"DMSpeaks Lite.{uuid4().hex}.lnk"
        checked("Create Startup shortcut", ole.CoCreateInstance(
            win.byref(shell_link_class), None, 1, win.byref(shell_link_interface), win.byref(link)))
        # IShellLinkW vtable slots from shobjidl.h.
        for index, operation, value in (
            (20, "Set shortcut path", str(launcher)),
            (11, "Set shortcut arguments", subprocess.list2cmdline(arguments)),
            (9, "Set shortcut working directory", str(directory)),
            (7, "Set shortcut description", "DMSpeaks Lite - WOW request listener"),
        ):
            checked(operation, call(link, index, HResult, [String], value))
        checked("Get shortcut persistence interface", call(
            link, 0, HResult, [win.POINTER(Guid), win.POINTER(Ptr)],
            win.byref(persist_file_interface), win.byref(persisted)))
        # IPersistFile::Save is slot 6. The staged file shares the final volume.
        checked("Save Startup shortcut", call(persisted, 6, HResult, [String, win.c_int], str(staged), 0))
        staged.replace(startup / "DMSpeaks Lite.lnk")
    finally:
        if persisted:
            call(persisted, 2, DWord, [])
        if link:
            call(link, 2, DWord, [])
        if folder:
            ole.CoTaskMemFree(folder)
        ole.CoUninitialize()
        if staged is not None:
            staged.unlink(missing_ok=True)


def register_background(launcher, script, root, port, folder, *, start_now, prepared_receipt=None):
    if not (root / "Interface/AddOns/DMSpeaksLite/DMSpeaksLite.toc").is_file():
        raise ValueError("Install the DMSpeaks add-on in the selected WoW client first.")
    if not launcher.is_file() or not script.is_file():
        raise ValueError("The installed DMSpeaks companion is incomplete.")
    launcher = launcher.resolve() if sys.platform == "win32" else launcher.absolute()
    script, root = script.resolve(), root.resolve()
    if prepared_receipt is not None:
        from lite_portable_package import bootstrap_playback
        bootstrap_playback(root / "Interface" / "AddOns")
    arguments = [*launch_arguments(script, root, port, folder, prepared_receipt), "--background"]
    if sys.platform != "win32":
        command = [str(launcher), *arguments]
        register_posix_background(command, script.parent.parent)
        if start_now:
            return subprocess.Popen(command, cwd=script.parent.parent, start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        return None
    register_windows_background(launcher, arguments, script.parent.parent)
    if start_now:
        return subprocess.Popen([str(launcher), *arguments], cwd=script.parent.parent,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_or_open(port, root, folder, plans, plans_sha256, *, show_settings=True, paced=None, store_managed=False):
    def ready():
        if show_settings:
            open_settings(port)
        else:
            print(f"DMSpeaks Lite is already listening on port {port}.", flush=True)

    build = companion_build_id()
    with ExitStack() as cleanup:
        if port == 0:
            port, state = automatic_listener(root, plans_sha256, build, folder, cleanup)
        else:
            state = listener_state(port, root, plans_sha256, build, folder=folder)
        if state == "ready":
            ready()
            return
        if state == "absent":
            check_deps()
            from lite_http import serve

            try:
                serve(port=port, folder=folder, on_ready=open_settings if show_settings else None,
                      connection_file=ensure_config(folder),
                      wow_root=root, game_plans=plans, game_plans_sha256=plans_sha256,
                      game_paced=paced, store_managed=store_managed)
                return
            except OSError as error:
                # An already starting listener can bind the port after the probe.
                if port == 0 or (error.errno not in {errno.EADDRINUSE, 10048}
                                 and getattr(error, 'winerror', None) != 10048):
                    raise
    deadline = monotonic() + 4
    while monotonic() < deadline and state not in DIFFERENT_LISTENER:
        state = listener_state(port, root, plans_sha256, build, folder=folder)
        if state == "ready":
            ready()
            return
        sleep(0.2)
    if state == "outdated":
        raise ValueError("An earlier DMSpeaks Lite listener is open. Close it before starting this preview.")
    if state == "different-catalog":
        raise ValueError("The running DMSpeaks listener uses a different dialogue catalog. Close it before starting this preview.")
    if state == "different-build":
        raise ValueError("A different DMSpeaks listener build is running. Run the new Lite setup, "
                         "then sign out and sign back in.")
    if state == "different-data":
        raise ValueError("DMSpeaks Lite is already running for this WoW client with a different data folder. "
                         "Close that companion before opening the requested data folder.")
    raise ValueError(f"Port {port} is already in use. Close the other companion or app and try again.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wow_root", type=Path)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--auto-port", action="store_true", help="Find a free local port during setup.")
    parser.add_argument("--prepared-inputs", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--register", action="store_true")
    mode.add_argument("--register-background", action="store_true")
    mode.add_argument("--setup", action="store_true", help="Install the add-on and register the shared listener.")
    mode.add_argument("--install", action="store_true", help="Register the DMSpeaks Lite name fetcher.")
    mode.add_argument("--background", action="store_true")
    args = parser.parse_args()
    if args.auto_port and (not args.install or not 1 <= args.port <= 65535):
        parser.error("--auto-port requires --install and a valid preferred port.")
    store_managed = args.prepared_inputs is None
    package = Path(__file__).resolve().parent.parent
    folder = args.data or default_data_folder()
    if args.register or args.register_background or args.setup or args.install:
        try:
            launcher = (package / "runtime/python/pythonw.exe" if sys.platform == "win32"
                        else Path(sys.executable))
            if args.register_background or args.setup or args.install:
                if store_managed:
                    _, catalog = store_inputs(args.wow_root)
                else:
                    _, catalog, _ = prepared_inputs(package, args.prepared_inputs)
                build = companion_build_id()
                if args.auto_port:
                    args.port, state = installer_listener(args.port, args.wow_root, catalog, build, folder)
                else:
                    state = listener_state(args.port, args.wow_root, catalog, build, folder=folder)
                if state == "other":
                    raise ValueError(f"Port {args.port} is already in use by another app.")
                if (args.install or args.setup) and not store_managed:
                    from lite_name_bridge import install_settings_addon
                    install_settings_addon(args.wow_root / "Interface" / "AddOns")
                previous_default_listener = (
                    state == "absent" and args.port != DEFAULT_PORT
                    and listener_state(DEFAULT_PORT, args.wow_root) in {"ready", "outdated"}
                )
                switch_at_sign_in = state in DIFFERENT_LISTENER or previous_default_listener
                child = register_background(launcher, Path(__file__), args.wow_root, args.port, folder,
                                            start_now=state == "absent" and not switch_at_sign_in,
                                            prepared_receipt=args.prepared_inputs)
                if child is not None:
                    setup_started = monotonic()
                    timeout = WINDOWS_LISTENER_STARTUP_TIMEOUT if sys.platform == "win32" else 20
                    deadline = setup_started + timeout
                    while True:
                        state = listener_state(args.port, args.wow_root, catalog, build, folder=folder)
                        if state == "ready":
                            break
                        child_status = child.poll()
                        if child_status is not None or monotonic() >= deadline:
                            elapsed = monotonic() - setup_started
                            termination = ("is still running" if child_status is None
                                           else f"exited with code {child_status}")
                            raise ValueError(
                                f"DMSpeaks did not become ready after {elapsed:.1f} seconds. "
                                f"The listener {termination}. See {folder / 'listener.log'}"
                            )
                        sleep(0.25)
                    print(f"DMSpeaks listener became ready in {monotonic() - setup_started:.1f} seconds.")
                if switch_at_sign_in:
                    system = " of Windows" if sys.platform == "win32" else ""
                    print(f"Sign out{system} and sign back in before using the new DMSpeaks listener in WoW.")
                else:
                    print("DMSpeaks is ready. In WoW, open Options > AddOns > DMSpeaks > Fetch Settings.")
            if args.register or ((args.setup or args.install) and not store_managed):
                register_integration(desktop_integration_directory(), launcher, Path(__file__),
                                     args.wow_root, args.port, folder, prepared_receipt=args.prepared_inputs)
                print("Registered under Help > External Integrations in DMSpeaks.")
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            parser.exit(1, str(error) + "\n")
        return 0
    runtime_bin = package / "runtime" / "bin"
    os.environ["PATH"] = str(runtime_bin) + os.pathsep + os.environ["PATH"]
    log_path = folder / ("listener.log" if args.background else "desktop-startup.log")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", buffering=1) as log, redirect_stdout(log), redirect_stderr(log):
            if store_managed:
                plans, catalog = store_inputs(args.wow_root)
                paced = None
            else:
                plans, catalog, paced = prepared_inputs(package, args.prepared_inputs)
            start_or_open(args.port, args.wow_root, folder, plans, catalog,
                          show_settings=not args.background, paced=paced, store_managed=store_managed)
    except (ImportError, OSError, SystemExit, ValueError) as error:
        action = "start the WoW listener" if args.background else "open WoW Fetch Settings"
        message = f"Could not {action}.\n{error}\n\nStartup log: {log_path}"
        try:
            with log_path.open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
        except OSError:
            pass
        if args.background:
            if sys.stderr is not None:
                print(message, file=sys.stderr)
            return 1
        if os.name == "nt":
            from ctypes import windll
            windll.user32.MessageBoxW(None, message, "DMSpeaks", 0x10)
        else:
            parser.exit(1, message + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
