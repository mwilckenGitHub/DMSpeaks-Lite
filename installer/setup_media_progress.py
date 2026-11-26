"""Show the staging helper's archive progress in the native Windows installer."""

from ctypes import byref, c_size_t, c_void_p, c_wchar_p, cast, wintypes
import os
from pathlib import Path
from runpy import run_path
import subprocess
import sys
from time import monotonic


MIB = 1024 * 1024
UPDATE_INTERVAL = 0.2  # Batch redraws when the server delivers several chunks at once.
STATUS_CONTROL = 1906  # The status label beside the progress control in Setup.nsi.
PBM_SETPOS = 0x0402
PBM_SETRANGE32 = 0x0406
BM_GETCHECK = 0x00F0
CANCELED = 4
WM_SETTEXT = 0x000C
SMTO_ABORTIFHUNG = 2
STEPS_PER_PERCENT = 100  # Keep the bar responsive even during multi-gigabyte downloads.


def window_api():
    """Load the Windows window-message helpers with pointer-sized arguments."""
    from ctypes import WinDLL

    user = WinDLL("user32")
    user.SendMessageTimeoutW.argtypes = (
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, c_void_p,
        wintypes.UINT, wintypes.UINT, wintypes.LPVOID,
    )
    user.SendMessageTimeoutW.restype = wintypes.LPARAM
    user.EnableWindow.argtypes = (wintypes.HWND, wintypes.BOOL)
    return user


def media_progress(window, end):
    """Return a callback that maps archive bytes to the first part of Setup's bar."""
    if not window or os.name != "nt":
        return None

    user = window_api()
    user.GetParent.argtypes = (wintypes.HWND,)
    user.GetParent.restype = wintypes.HWND
    user.GetDlgItem.argtypes = (wintypes.HWND, wintypes.INT)
    user.GetDlgItem.restype = wintypes.HWND
    status = user.GetDlgItem(user.GetParent(window), STATUS_CONTROL)
    send = user.SendMessageTimeoutW
    send(window, PBM_SETRANGE32, 0, c_void_p(100 * STEPS_PER_PERCENT), SMTO_ABORTIFHUNG, 200, None)
    last_position = -1
    last_archive = None
    next_update = 0.0

    def report(action, index, count, archive_done, archive_size, total_done, total_size):
        """Refresh the current archive label and the overall preparation meter."""
        nonlocal last_position, last_archive, next_update
        now = monotonic()
        archive = (action, index)
        if archive == last_archive and now < next_update:
            return
        last_archive = archive
        next_update = now + UPDATE_INTERVAL
        position = end * STEPS_PER_PERCENT * total_done // max(1, total_size)
        if position != last_position:
            if send(window, PBM_SETPOS, position, None, SMTO_ABORTIFHUNG, 200, None):
                last_position = position
        if action == "downloading":
            label = (f"Downloading {index} of {count}  |  "
                     f"{archive_done / MIB:,.1f} / {archive_size / MIB:,.1f} MiB")
        elif action == "unpacking":
            label = "Preparing speech data..."
        else:
            label = f"Checking archive {index} of {count}..."
        if status:
            # WM_SETTEXT is marshalled across processes. Bound the wait if the wizard is busy.
            text = c_wchar_p(label)
            send(status, WM_SETTEXT, 0, cast(text, c_void_p), SMTO_ABORTIFHUNG, 200, None)

    return report


def run_staging(arguments, progress):
    """Run the staging script under the same isolated Python interpreter."""
    # Embedded Python runs in isolation, so resolve the companion script explicitly.
    staging = run_path(str(Path(__file__).with_name("stage_release_media.py")))
    staging["main"](arguments, progress=progress)


def supervise_staging(window, end, button, arguments):
    """Cancel preparation independently of a stalled network or archive read."""
    if not button or os.name != "nt":
        run_staging(arguments, media_progress(window, end))
        return 0

    user = window_api()

    def canceled():
        checked = c_size_t()
        return (user.SendMessageTimeoutW(button, BM_GETCHECK, 0, None, SMTO_ABORTIFHUNG, 200, byref(checked))
                and checked.value)

    command = [sys.executable, "-I", "-B", __file__, "--worker", str(window), str(end), *arguments]
    with subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW) as worker:
        while True:
            if canceled():
                worker.kill()
                worker.wait()
                # Only the unfinished transfer owned by this worker is removed.
                staging = run_path(str(Path(__file__).with_name("stage_release_media.py")))
                prefix = staging["pending_download_prefix"](worker.pid)
                for path in Path(arguments[2]).glob(prefix + "*.part"):
                    path.unlink(missing_ok=True)
                return CANCELED
            try:
                status = worker.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                continue
            user.EnableWindow(button, False)
            return CANCELED if canceled() else status


if __name__ == "__main__":
    if sys.argv[1] == "--worker":
        run_staging(sys.argv[4:], media_progress(int(sys.argv[2]), int(sys.argv[3])))
    else:
        raise SystemExit(supervise_staging(*(int(value) for value in sys.argv[1:4]), sys.argv[4:]))
