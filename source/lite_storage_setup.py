"""Hand storage authentication and folder selection to the user's desktop session."""
import ctypes
import os
from pathlib import Path, PureWindowsPath
import shutil
import socket
import subprocess
import sys
from threading import Lock
from urllib.parse import quote
import webbrowser

from lite_store_playback import STATUS

PICKER = Lock()
NETWORK = {"nas", "network", "tailscale"}
CLOUD_HELP = {
    "googledrive": "https://support.google.com/a/users/answer/13022292",
    "onedrive": "https://support.microsoft.com/en-us/onedrive/sync-your-computer-s-files-and-folders-with-onedrive",
    "icloud": "https://support.apple.com/icloud",
    "dropbox": "https://www.dropbox.com/install",
}


def location_label(path):
    drive = PureWindowsPath(path).drive
    if drive.startswith(chr(92) * 2):
        return drive.strip(chr(92)).replace(chr(92), "/")
    return Path(path).name or str(path)


def local_location(addons):
    path = str(addons / STATUS)
    label = location_label(path) if PureWindowsPath(path).drive.startswith(chr(92) * 2) else socket.gethostname()
    return {"state": "local", "location": label, "path": path}


def choose_folder():
    if not PICKER.acquire(blocking=False):
        raise ValueError("A folder window is already open.")
    try:
        if os.name == "nt":
            script = """[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Choose a name card folder'
if ($d.ShowDialog() -eq 'OK') { [Console]::Write($d.SelectedPath) }
$d.Dispose()"""
            command = ["powershell.exe", "-NoProfile", "-STA", "-Command", script]
        elif sys.platform == "darwin":
            command = ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose a name card folder")']
        elif shutil.which("zenity"):
            command = ["zenity", "--file-selection", "--directory", "--title=Choose a name card folder"]
        else:
            raise ValueError("Enter the mounted folder path in Setup.")
        picked = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace")
        if picked.returncode and not (picked.returncode == 1 and (
                not picked.stderr.strip() or "(-128)" in picked.stderr or sys.platform.startswith("linux")
                and "display" not in picked.stderr.lower())):
            raise ValueError("The folder window could not open. Enter the folder path in Setup.")
        return {"folder": picked.stdout.strip() if picked.returncode == 0 else ""}
    finally:
        PICKER.release()


def connect_storage(medium, address=""):
    if medium in NETWORK:
        # Only a user-supplied SMB server/share is passed to the file manager.
        parts = (address or "").strip().replace(chr(92), "/").removeprefix("smb://").strip("/").split("/")
        if (len(parts) < 2 or any(not p or p in (".", "..") or any(c in p for c in '@?:#%') for p in parts)
                or any(ord(c) < 32 for p in parts for c in p)):
            raise ValueError("Enter a network address such as server/share.")
        if os.name == "nt":
            unc = (chr(92) * 2) + chr(92).join(parts)
            # Explorer handles password prompts using Windows Credential Manager.
            subprocess.Popen(["explorer.exe", unc])
        else:
            address = "smb://" + "/".join(quote(p, safe="-._") for p in parts)
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", address])
        return {"opened": "network"}
    if medium not in CLOUD_HELP:
        raise ValueError("Choose a cloud service or a network share.")
    if os.name == "nt":
        folders = [Path(os.environ[k]) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
                   if os.environ.get(k)]
        patterns = {"googledrive": ["Google/Drive File Stream/*/GoogleDriveFS.exe"],
                    "onedrive": ["Microsoft OneDrive/OneDrive.exe", "Microsoft/OneDrive/OneDrive.exe"],
                    "dropbox": ["Dropbox/Client/Dropbox.exe"], "icloud": []}
        for folder in folders:
            for pattern in patterns[medium]:
                for app in sorted(folder.glob(pattern), reverse=True):
                    if app.is_file():
                        subprocess.Popen([str(app)])
                        return {"opened": "provider"}
        if medium == "icloud":
            apps = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                "(Get-StartApps | Where-Object { $_.AppID -like 'AppleInc.iCloud_*!*' } | Select-Object -First 1).AppID"],
                capture_output=True, text=True)
            application_id = apps.stdout.strip()
            if apps.returncode == 0 and application_id.startswith("AppleInc.iCloud_") and "!" in application_id:
                subprocess.Popen(["explorer.exe", "shell:AppsFolder" + chr(92) + application_id])
                return {"opened": "provider"}
    elif sys.platform == "darwin":
        apps = {"googledrive": "Google Drive", "onedrive": "OneDrive", "dropbox": "Dropbox"}
        if medium == "icloud":
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preferences.AppleIDPrefPane"])
            return {"opened": "provider"}
        if subprocess.run(["open", "-a", apps[medium]], capture_output=True).returncode == 0:
            return {"opened": "provider"}
    elif medium == "dropbox" and shutil.which("dropbox"):
        subprocess.Popen(["dropbox", "start"])
        return {"opened": "provider"}
    elif medium in {"googledrive", "onedrive"} and shutil.which("gnome-control-center"):
        subprocess.Popen(["gnome-control-center", "online-accounts"])
        return {"opened": "provider"}
    webbrowser.open(CLOUD_HELP[medium])
    return {"opened": "help"}


def provider_confirmed(paths):
    """Read Cloud Files' provider acknowledgement. A successful local write alone is ambiguous."""
    if os.name != "nt":
        return False
    try:
        state_from_tag = ctypes.windll.cldapi.CfGetPlaceholderStateFromAttributeTag
        state_from_tag.restype = ctypes.c_uint32
        for path in paths:
            stat = path.lstat()
            state = state_from_tag(stat.st_file_attributes, stat.st_reparse_tag)
            if state == 0xffffffff or state & 0x09 != 0x09:
                return False
        return True
    except (AttributeError, OSError):
        return False
