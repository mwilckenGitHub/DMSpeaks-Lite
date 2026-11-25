"""Open Lite and retire an idle predecessor from the same Windows installation."""

from contextlib import closing
import ctypes
from ctypes import wintypes
from hashlib import sha256
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import struct
import sys
from urllib.parse import urlencode

LOOPBACK = "127.0.0.1"
READY = "/api/wow-fetch-ready"
PROTOCOL = 4
IDLE_STATES = {"connected", "paused", "complete", "coverage", "preview", "unavailable"}
BUILD_NAME = re.compile(r"[0-9a-f]{64}\Z")
INSTALL_NAME = re.compile(r"[0-9a-f]{20}\Z")


def read_snapshot(path):
    if path.is_symlink():
        return None, None
    try:
        with path.open("rb") as stream:
            text = stream.read(8192)
    except FileNotFoundError:
        return None, None
    port = re.search(rb"^    settingsPort = ([0-9]{1,5}),\r?$", text, re.M)
    state = re.search(rb'^    state = "([a-z]+)",\r?$', text, re.M)
    return int(port[1]) if port else None, state[1].decode("ascii") if state else None


def probe(port, game, data, client_id):
    query = urlencode({"client": client_id(game), "data": client_id(data)})
    try:
        with closing(HTTPConnection(LOOPBACK, port, timeout=1.5)) as connection:
            connection.request("GET", READY + "?" + query)
            response = connection.getresponse()
            result = json.loads(response.read(1024)) if response.status == 200 else None
    except (OSError, ValueError, HTTPException):
        return None
    if (isinstance(result, dict) and result.get("ready") is True and result.get("protocol") == PROTOCOL
            and result.get("data_matches") is True and isinstance(result.get("build"), str)
            and BUILD_NAME.fullmatch(result["build"])):
        return result
    return None


def listening_pid(port):
    """Find the owning PID of the one IPv4 listener bound to this exact loopback port."""
    get_table = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    get_table.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    )
    get_table.restype = wintypes.DWORD
    size = wintypes.DWORD()
    buffer = None
    for _ in range(3):
        result = get_table(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if result == 0 and buffer is not None:
            break
        if result != 122 or not 4 <= size.value <= 16 * 1024**2:
            raise OSError(f"Could not check the local listener owner (Windows error {result}).")
        buffer = ctypes.create_string_buffer(size.value)
    else:
        raise OSError("Windows could not identify the local listener owner.")

    row_size = struct.calcsize("<6I")
    count, = struct.unpack_from("<I", buffer)
    if 4 + count * row_size > len(buffer):
        raise OSError("Windows returned an incomplete local listener table.")
    address = int.from_bytes(socket.inet_aton(LOOPBACK), "little")
    owners = {pid for _, local, raw_port, _, _, pid in
              struct.iter_unpack("<6I", buffer.raw[4:4 + count * row_size])
              if local == address and socket.ntohs(raw_port & 0xFFFF) == port}
    return owners.pop() if len(owners) == 1 else None


def installed_build_id(package):
    """Check the predecessor's identity with the app's existing build hash format."""
    root = package / "source"
    files = list(root.glob("*.py"))
    for directory in ("formats", "lite_addon", "lite_web", "third_party"):
        files.extend(path for path in (root / directory).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    build = sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        build.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        build.update(sha256(path.read_bytes()).digest())
    for name in ("Prepared-dialogue-plans.sha256", "Prepared-paced-deliveries.json"):
        pin = package / name
        if pin.is_file():
            build.update(name.encode("ascii") + b"\0" + sha256(pin.read_bytes()).digest())
    return build.hexdigest()


def matches_install(image, builds, expected_build):
    try:
        relative = image.relative_to(builds)
    except ValueError:
        return False
    parts = relative.parts
    if (len(parts) != 4 or not INSTALL_NAME.fullmatch(parts[0])
            or tuple(part.casefold() for part in parts[1:]) != ("runtime", "python", "pythonw.exe")
            or image.resolve() != image):
        return False
    package = builds / parts[0]
    return (package / "source/wow_fetch_desktop.py").is_file() and installed_build_id(package) == expected_build


def replace_process(pid, builds, expected_build, still_owns_listener):
    """Hold one process handle across identity verification and termination."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    )
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    access = 0x1000 | 0x0001 | 0x100000  # query limited information, terminate, synchronize
    handle = kernel.OpenProcess(access, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        path = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(path))
        if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not matches_install(Path(path.value), builds, expected_build) or not still_owns_listener():
            return False
        if not kernel.TerminateProcess(handle, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if kernel.WaitForSingleObject(handle, 8000) != 0:
            raise OSError("The earlier DMSpeaks Lite process has not closed yet.")
        return True
    finally:
        kernel.CloseHandle(handle)


def switch_previous_listener(package, game, data, build, client_id):
    """Retire an idle listener for this client, keeping the requested build if set."""
    next_step = "continue uninstalling" if build is None else "open DMSpeaks Lite again"
    addons = game / "Interface/AddOns"
    snapshots = (addons / "DMSpeaksLiteStatus/Snapshot.lua", addons / "DMSpeaksLite/Status.lua")
    for snapshot in snapshots:
        port, _ = read_snapshot(snapshot)
        if not port or not port <= 65535:
            continue
        old = probe(port, game, data, client_id)
        if not old or old["build"] == build:
            continue
        pid = listening_pid(port)
        if not pid or pid == os.getpid():
            continue
        database = data / "studio.sqlite"
        if not database.is_file():
            continue
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=0.6)) as db:
            # Keep the SQLite writer slot until the old process exits. A new
            # fetch records its job here before submitting any requests.
            db.execute("BEGIN IMMEDIATE")
            record = db.execute("SELECT value FROM settings WHERE key = ?", ("lite_game_fetch_inflight",)).fetchone()
            current_port, state = read_snapshot(snapshot)
            if (record and json.loads(record[0])) or state in {"fetching", "aligning"}:
                raise ValueError(f"Pause name fetching and wait for Lite to report Paused, then {next_step}.")
            if current_port != port or state not in IDLE_STATES:
                raise ValueError(f"Wait for the current DMSpeaks task to finish, then {next_step}.")

            def still_owns_listener():
                return listening_pid(port) == pid and probe(port, game, data, client_id) == old

            if replace_process(pid, package.parent, old["build"], still_owns_listener):
                return True
    return False


def main():
    package = Path(sys.executable).resolve().parents[2]
    sys.path.insert(0, str(package / "source"))
    from wow_fetch_desktop import (client_id, companion_build_id, default_data_folder, listener_state,
                                   main as launch, store_inputs, windows_runtime_listener_ports)

    uninstall = sys.argv[1:2] == ["--stop-for-uninstall"]
    game = Path(sys.argv[2] if uninstall else sys.argv[1]).resolve()
    data = default_data_folder()
    if uninstall:
        try:
            stopped = switch_previous_listener(package, game, data, None, client_id)
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"Could not close DMSpeaks Lite. {error}", flush=True)
            return 1
        if stopped:
            print("Closed the idle DMSpeaks Lite process.", flush=True)
        return 0
    try:
        # An in-place repair can leave this install's listener serving the same catalog.
        try:
            _, catalog = store_inputs(game)
        except (OSError, ValueError):
            catalog = None
        already_running = catalog is not None and any(
            listener_state(port, game, catalog, folder=data) == "ready"
            for port in windows_runtime_listener_ports()
        )
        restarted = not already_running and switch_previous_listener(
            package, game, data, companion_build_id(), client_id)
        if restarted:
            try:
                with (data / "desktop-startup.log").open("a", encoding="utf-8") as log:
                    print("Replaced the previous idle DMSpeaks Lite process.", file=log)
            except OSError:
                pass
    except (OSError, ValueError, sqlite3.Error) as error:
        message = f"Could not switch to the installed DMSpeaks Lite.\n{error}"
        ctypes.windll.user32.MessageBoxW(None, message, "DMSpeaks", 0x10)
        return 1
    sys.argv = [sys.argv[0], "--port", "0", "--", str(game)]
    return launch()


if __name__ == "__main__":
    sys.exit(main())
