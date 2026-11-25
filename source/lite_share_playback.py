"""Keep WoW's pre-playback availability check independent of a stalled network mount."""
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
from stat import S_ISREG
from threading import Lock
from time import time
from uuid import uuid4

from lite_portable_io import cache_bytes
from lite_store_playback import STATUS, safe_path

LIBRARY = "DMSpeaks-WoW-Name-Cards"
PROBE = Path(__file__).with_name("lite_addon") / "ShareProbe.ogg"
WINDOW = 15  # Shared with Playback.lua. Two slots allow a clock-boundary crossing.
NAME_SURROGATE = 0x20000000  # Windows reparse tag bit for redirects such as junctions.


def probe_audio():
    """Use the packaged probe, or encode sixty milliseconds of digital silence."""
    if PROBE.is_file():
        return PROBE.read_bytes()
    from io import BytesIO

    import numpy as np
    import soundfile as sf

    output = BytesIO()
    sf.write(output, np.zeros(1323, dtype=np.float32), 22050, format="OGG", subtype="VORBIS")
    return output.getvalue()


def mounted_network_folder(path):
    if os.name == "nt":
        import ctypes
        drive = PureWindowsPath(path).anchor
        return drive.startswith(chr(92) * 2) or ctypes.windll.kernel32.GetDriveTypeW(str(drive)) == 4
    if sys.platform == "darwin":
        result = subprocess.run(["stat", "-f", "%T", str(path)], capture_output=True, text=True)
        return result.returncode == 0 and result.stdout.strip() in {"smbfs", "nfs", "webdav", "macfuse", "osxfuse"}
    if sys.platform.startswith("linux"):
        # The longest containing mount wins, including a local mount below an SMB mount.
        mounts = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, _, right = line.partition(" - ")
            root = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), left.split()[4]))
            if path.is_relative_to(root):
                mounts.append((len(root.parts), root, right.split()[0]))
        if mounts:
            _, root, fs = max(mounts)
            if fs == "fuse.gvfsd-fuse":
                subpath = path.relative_to(root).parts
                return bool(subpath and subpath[0].startswith(("smb-share:", "sftp:", "dav:")))
            return fs in {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs", "davfs"}
    return False


def read_shared_file(path, limit):
    # Reject redirects, devices and oversized objects before consuming remote data.
    if path.resolve() != path:
        raise ValueError("The selected library contains a redirected file.")
    entry = path.lstat()
    if not S_ISREG(entry.st_mode) or getattr(entry, "st_reparse_tag", 0) & NAME_SURROGATE:
        raise ValueError("Choose an ordinary file in the shared library.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as remote:
        info = os.fstat(remote.fileno())
        # A hydrated OneDrive file can keep its cloud reparse tag. Compare the opened
        # file's identity to the selected entry so ordinary cloud placeholders work.
        if not S_ISREG(info.st_mode) or not os.path.samestat(entry, info) or info.st_size > limit:
            raise ValueError("The selected library contains an invalid file.")
        data = remote.read(limit + 1)
        if len(data) > limit:
            raise ValueError("A shared library file exceeds its size limit.")
        return data


def bind_folder(folder):
    library = folder / LIBRARY
    if library.is_symlink():
        raise ValueError("Choose a shared folder with an ordinary name card library.")
    library.mkdir(exist_ok=True)
    token = uuid4().hex
    with (library / ("device-" + token)).open("xb") as output:
        output.write(token.encode("ascii"))
    return token


def check_folder(config):
    token = config.get("binding", "")
    if not re.fullmatch("[0-9a-f]{32}", token):
        raise ValueError("Choose the shared folder again in Name card storage.")
    marker = Path(config["folder"]) / LIBRARY / ("device-" + token)
    try:
        if read_shared_file(marker, 32) == token.encode("ascii"):
            return
    except OSError:
        pass
    if config.get("cache_offline"):
        raise ValueError("The shared name library is offline. WoW can still play name recordings saved on this computer.")
    raise ValueError("The shared name library is offline. NPCs will use Adventurer until it reconnects.")


class SharePlayback:
    def __init__(self, addons):
        self.folder = safe_path(addons, STATUS / "SharePlayback")
        self.lock = Lock()
        self.probe = probe_audio()

    def publish(self, required, reachable):
        # WoW can probe ordinary audio assets before playback. Time-limited silent
        # Oggs expire even if Lite exits or a filesystem call never returns.
        with self.lock:
            if not required and not self.folder.exists():
                return
            if self.folder.is_symlink():
                raise ValueError("The local playback status folder is linked.")
            self.folder.mkdir(parents=True, exist_ok=True)
            mode = self.folder / "shared.ogg"
            if required and not mode.exists():
                cache_bytes(mode, self.probe)
            slot = int(time()) // WINDOW
            active = {f"ready-{i}.ogg" for i in (slot, slot + 1)} if required and reachable else set()
            for name in active:
                if not (self.folder / name).exists():
                    cache_bytes(self.folder / name, self.probe)
            for path in self.folder.glob("ready-*.ogg"):
                if path.name not in active and re.fullmatch(r"ready-[0-9]+\.ogg", path.name):
                    path.unlink(missing_ok=True)
            if not required:
                mode.unlink(missing_ok=True)
