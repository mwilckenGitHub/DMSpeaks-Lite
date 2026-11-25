"""Draw Lite's passive progress window above the configured WoW client."""

from ctypes import POINTER, Structure, byref, c_int, c_ssize_t, c_ubyte, c_void_p
from ctypes import create_unicode_buffer, wintypes as win
from dataclasses import dataclass
import ctypes
import logging
import math
import os
from pathlib import Path
import sys
from threading import Event, Thread, current_thread
from time import monotonic


LOG = logging.getLogger(__name__)
TICK_SECONDS = 0.075
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
HWND_TOPMOST = -1
SW_HIDE = 0
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOOWNERZORDER = 0x0200
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
PASSIVE_STYLE = WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE
WM_ERASEBKGND = 0x0014
WM_PAINT = 0x000F
WM_QUIT = 0x0012
WM_MOUSEACTIVATE = 0x0021
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1
MA_NOACTIVATE = 3
PM_REMOVE = 1
LWA_ALPHA = 2
SRCCOPY = 0x00CC0020
GDI_ERROR = c_void_p(-1).value
DRAW_TEXT = 0x0020 | 0x0004 | 0x8000 | 0x0800  # Single line, centered vertically, ellipsis, literal ampersand.
CORNERS = {"BOTTOMRIGHT": (92, 104), "BOTTOMLEFT": (22, 228), "TOPLEFT": (22, 112)}
SPINNER = tuple((math.cos(i * math.tau / 10), math.sin(i * math.tau / 10)) for i in range(10))


class _Message(Structure):
    _fields_ = [
        ("hwnd", win.HWND), ("message", win.UINT), ("wParam", win.WPARAM), ("lParam", win.LPARAM),
        ("time", win.DWORD), ("pt", win.POINT), ("lPrivate", win.DWORD),
    ]


class _Paint(Structure):
    _fields_ = [
        ("hdc", win.HDC), ("erase", win.BOOL), ("rect", win.RECT), ("restore", win.BOOL),
        ("update", win.BOOL), ("reserved", c_ubyte * 32),
    ]


class _API:
    def __init__(self):
        self.callback_type = ctypes.WINFUNCTYPE(c_ssize_t, win.HWND, win.UINT, win.WPARAM, win.LPARAM)

        class WindowClass(Structure):
            _fields_ = [
                ("style", win.UINT), ("proc", self.callback_type), ("class_extra", c_int),
                ("window_extra", c_int), ("module", win.HINSTANCE), ("icon", win.HICON),
                ("cursor", win.HANDLE), ("background", win.HBRUSH),
                ("menu_name", win.LPCWSTR), ("class_name", win.LPCWSTR),
            ]

        self.window_class = WindowClass
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        hwnd, hdc, hobj = win.HWND, win.HDC, win.HGDIOBJ
        boolean, uint, rect = win.BOOL, win.UINT, POINTER(win.RECT)
        self._bind(user, "RegisterClassW", win.ATOM, POINTER(WindowClass))
        self._bind(user, "UnregisterClassW", boolean, win.LPCWSTR, win.HINSTANCE)
        self._bind(user, "CreateWindowExW", hwnd, win.DWORD, win.LPCWSTR, win.LPCWSTR, win.DWORD,
                   c_int, c_int, c_int, c_int, hwnd, win.HMENU, win.HINSTANCE, c_void_p)
        self._bind(user, "DestroyWindow", boolean, hwnd)
        self._bind(user, "DefWindowProcW", c_ssize_t, hwnd, uint, win.WPARAM, win.LPARAM)
        self._bind(user, "PeekMessageW", boolean, POINTER(_Message), hwnd, uint, uint, uint)
        self._bind(user, "DispatchMessageW", c_ssize_t, POINTER(_Message))
        self._bind(user, "GetForegroundWindow", hwnd)
        self._bind(user, "GetAncestor", hwnd, hwnd, uint)
        self._bind(user, "GetWindowThreadProcessId", win.DWORD, hwnd, POINTER(win.DWORD))
        self._bind(user, "IsWindowVisible", boolean, hwnd)
        self._bind(user, "IsIconic", boolean, hwnd)
        self._bind(user, "GetClientRect", boolean, hwnd, rect)
        self._bind(user, "ClientToScreen", boolean, hwnd, POINTER(win.POINT))
        self._bind(user, "SetWindowPos", boolean, hwnd, hwnd, c_int, c_int, c_int, c_int, uint)
        self._bind(user, "ShowWindow", boolean, hwnd, c_int)
        self._bind(user, "SetLayeredWindowAttributes", boolean, hwnd, win.COLORREF, c_ubyte, win.DWORD)
        self._bind(user, "InvalidateRect", boolean, hwnd, rect, boolean)
        self._bind(user, "BeginPaint", hdc, hwnd, POINTER(_Paint))
        self._bind(user, "EndPaint", boolean, hwnd, POINTER(_Paint))
        self._bind(user, "FillRect", c_int, hdc, rect, win.HBRUSH)
        self._bind(user, "DrawTextW", c_int, hdc, win.LPCWSTR, c_int, rect, uint)
        self._bind(kernel, "GetModuleHandleW", win.HMODULE, win.LPCWSTR)
        self._bind(kernel, "OpenProcess", win.HANDLE, win.DWORD, boolean, win.DWORD)
        self._bind(kernel, "QueryFullProcessImageNameW", boolean, win.HANDLE, win.DWORD,
                   win.LPWSTR, POINTER(win.DWORD))
        self._bind(kernel, "CloseHandle", boolean, win.HANDLE)
        self._bind(gdi, "CreateCompatibleDC", hdc, hdc)
        self._bind(gdi, "DeleteDC", boolean, hdc)
        self._bind(gdi, "CreateCompatibleBitmap", win.HBITMAP, hdc, c_int, c_int)
        self._bind(gdi, "BitBlt", boolean, hdc, c_int, c_int, c_int, c_int, hdc, c_int, c_int, win.DWORD)
        self._bind(gdi, "CreateSolidBrush", win.HBRUSH, win.COLORREF)
        self._bind(gdi, "DeleteObject", boolean, hobj)
        self._bind(gdi, "SelectObject", hobj, hdc, hobj)
        self._bind(gdi, "SetBkMode", c_int, hdc, c_int)
        self._bind(gdi, "SetTextColor", win.COLORREF, hdc, win.COLORREF)
        self._bind(gdi, "CreateFontW", win.HFONT, *([c_int] * 5), *([win.DWORD] * 8), win.LPCWSTR)
        self.get_dpi = self._bind(user, "GetDpiForWindow", uint, hwnd, optional=True)
        self.thread_dpi = self._bind(user, "SetThreadDpiAwarenessContext", c_void_p, c_void_p, optional=True)

    def _bind(self, library, name, result, *args, optional=False):
        fn = getattr(library, name, None)
        if fn is None and optional:
            return None
        if fn is None:
            raise AttributeError(name)
        fn.restype = result
        fn.argtypes = args
        setattr(self, name, fn)
        return fn


def _color(red, green, blue):
    return red | green << 8 | blue << 16


def _text(value):
    return " ".join(str(value or "").replace("\0", "").split())[:256]


@dataclass(frozen=True)
class _View:
    title: str
    count: str
    occurrence: str
    detail: str
    percent: float | None
    opacity: int
    minimized: bool
    working: bool
    corner: str
    reload_hint: str = ""


def _view(sample):
    if not sample:
        return None
    percent = sample.get("percent")
    if percent is not None:
        percent = float(percent)
        percent = max(0, min(percent, 100)) if math.isfinite(percent) else None
    corner = sample.get("corner", "BOTTOMRIGHT")
    return _View(_text(sample.get("title")), _text(sample.get("count")),
                 _text(sample.get("occurrence")), _text(sample.get("detail")),
                 percent, max(25, min(int(sample.get("opacity", 90)), 100)),
                 bool(sample.get("minimized")), bool(sample.get("working")),
                 corner if corner in CORNERS else "BOTTOMRIGHT", _text(sample.get("reload_hint")))


def _scale(value, dpi):
    return (value * dpi + 48) // 96


def _position(left, top, right, bottom, dpi, view):
    client_width, client_height = right - left, bottom - top
    margin = _scale(12, dpi)
    width = min(client_width - 2 * margin, _scale(190 if view.minimized else 300, dpi))
    height = _scale((28 if view.minimized else 64) + (17 if view.reload_hint else 0), dpi)
    if width < _scale(126, dpi) or client_height < height + 2 * margin:
        return None

    offset_x, offset_y = (_scale(offset, dpi) for offset in CORNERS[view.corner])
    offset_x = min(offset_x, client_width - width - margin)
    offset_y = min(offset_y, client_height - height - margin)
    x = right - width - offset_x if view.corner == "BOTTOMRIGHT" else left + offset_x
    y = top + offset_y if view.corner == "TOPLEFT" else bottom - height - offset_y
    return x, y, width, height


def _image_path(path):
    return os.path.normcase(os.path.realpath(path))


class _Popup:
    def __init__(self, game_exe, sample):
        self.game_exe, self.sample = game_exe, sample
        self.stop, self.ready = Event(), Event()
        self.thread = Thread(target=self.run, name="DMSpeaksLiteProgress", daemon=True)
        self.api = None
        self.hwnd = None
        self.shown = False
        self.rect = None
        self.opacity = None
        self.view = None
        self.dpi = 96
        self.foreground_pid = None
        self.foreground_due = 0.0
        self.foreground_match = False

    def close(self):
        self.stop.set()
        if self.thread is not current_thread():
            self.thread.join(timeout=2)

    def run(self):
        registered = False
        previous_dpi = None
        try:
            self.api = api = _API()
            if api.thread_dpi:
                previous_dpi = api.thread_dpi(-4)  # Per-monitor DPI awareness v2 for this window thread.
            self.module = api.GetModuleHandleW(None)
            self.class_name = f"DMSpeaksLiteProgress_{id(self):x}"
            self.proc = api.callback_type(self.window_proc)
            registered = api.RegisterClassW(byref(api.window_class(
                proc=self.proc, module=self.module, class_name=self.class_name)))
            if not registered:
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = api.CreateWindowExW(PASSIVE_STYLE, self.class_name, "DMSpeaks Lite - WOW progress", WS_POPUP,
                                            0, 0, 1, 1, None, None, self.module, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            if not api.SetLayeredWindowAttributes(self.hwnd, 0, 230, LWA_ALPHA):
                raise ctypes.WinError(ctypes.get_last_error())
            self.opacity = 90
            self.ready.set()
            message = _Message()
            while not self.stop.is_set():
                while api.PeekMessageW(byref(message), None, 0, 0, PM_REMOVE):
                    if message.message == WM_QUIT:
                        return
                    api.DispatchMessageW(byref(message))
                self.update()
                self.stop.wait(TICK_SECONDS if self.shown else 0.18)
        except Exception:
            LOG.warning("The Windows progress display could not continue.", exc_info=True)
        finally:
            if self.hwnd:
                self.api.DestroyWindow(self.hwnd)
                self.hwnd = None
            if registered:
                self.api.UnregisterClassW(self.class_name, self.module)
            if previous_dpi:
                self.api.thread_dpi(previous_dpi)
            self.ready.set()

    def game_window(self):
        api = self.api
        foreground = api.GetForegroundWindow()
        if not foreground:
            return None
        root = api.GetAncestor(foreground, GA_ROOT)
        if not root or api.IsIconic(root) or not api.IsWindowVisible(root):
            return None
        pid = win.DWORD()
        foreground_pid = win.DWORD()
        if (not api.GetWindowThreadProcessId(root, byref(pid))
                or not api.GetWindowThreadProcessId(foreground, byref(foreground_pid))
                or not pid.value or pid.value != foreground_pid.value):
            return None
        now = monotonic()
        if pid.value != self.foreground_pid or now >= self.foreground_due:
            self.foreground_pid = pid.value
            self.foreground_due = now + 1.0
            self.foreground_match = self.matches_process(pid.value)
        return root if self.foreground_match else None

    def matches_process(self, pid):
        api = self.api
        handle = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            image = create_unicode_buffer(32768)
            size = win.DWORD(len(image))
            return bool(api.QueryFullProcessImageNameW(handle, 0, image, byref(size))
                        and _image_path(image.value) == self.game_exe)
        finally:
            api.CloseHandle(handle)

    def hide(self):
        if self.shown:
            self.api.ShowWindow(self.hwnd, SW_HIDE)
            self.shown = False

    def update(self):
        root = self.game_window()
        if root is None:
            self.hide()
            return
        try:
            view = _view(self.sample())
        except (OSError, ValueError, TypeError, AttributeError):
            self.hide()
            return
        if view is None:
            self.hide()
            return
        api = self.api
        client = win.RECT()
        if not api.GetClientRect(root, byref(client)):
            self.hide()
            return
        left_top = win.POINT(client.left, client.top)
        right_bottom = win.POINT(client.right, client.bottom)
        if (not api.ClientToScreen(root, byref(left_top))
                or not api.ClientToScreen(root, byref(right_bottom))):
            self.hide()
            return
        dpi = (api.get_dpi(root) or 96) if api.get_dpi else 96
        rect = _position(left_top.x, left_top.y, right_bottom.x, right_bottom.y, dpi, view)
        if rect is None:
            self.hide()
            return
        if self.opacity != view.opacity:
            if not api.SetLayeredWindowAttributes(self.hwnd, 0, round(view.opacity * 255 / 100), LWA_ALPHA):
                self.hide()
                return
            self.opacity = view.opacity
        repaint = not self.shown or view.working or view != self.view or self.dpi != dpi or self.rect != rect
        self.view, self.dpi = view, dpi
        if not self.shown or self.rect != rect:
            if not api.SetWindowPos(self.hwnd, HWND_TOPMOST, *rect,
                                    SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOOWNERZORDER):
                self.hide()
                return
            self.rect, self.shown = rect, True
        if repaint:
            api.InvalidateRect(self.hwnd, None, False)

    def window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_NCHITTEST:
            return HTTRANSPARENT
        if message == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if message == WM_ERASEBKGND:
            return 1
        if message == WM_PAINT:
            try:
                self.paint(hwnd)
            except Exception:
                self.stop.set()
                LOG.warning("The Windows progress display could not paint.", exc_info=True)
            return 0
        return self.api.DefWindowProcW(hwnd, message, wparam, lparam)

    def paint(self, hwnd):
        api = self.api
        paint = _Paint()
        dc = api.BeginPaint(hwnd, byref(paint))
        bitmap = buffer = old = None
        try:
            rect = win.RECT()
            if not dc or self.view is None or not api.GetClientRect(hwnd, byref(rect)):
                return
            width, height = rect.right, rect.bottom
            if not width or not height:
                return
            buffer = api.CreateCompatibleDC(dc)
            if buffer:
                bitmap = api.CreateCompatibleBitmap(dc, width, height)
                if bitmap:
                    old = api.SelectObject(buffer, bitmap)
            if old and old != GDI_ERROR:
                self.draw(buffer, width, height)
                api.BitBlt(dc, 0, 0, width, height, buffer, 0, 0, SRCCOPY)
            else:
                self.draw(dc, width, height)
        finally:
            if old and old != GDI_ERROR:
                api.SelectObject(buffer, old)
            if bitmap:
                api.DeleteObject(bitmap)
            if buffer:
                api.DeleteDC(buffer)
            if dc:
                api.EndPaint(hwnd, byref(paint))

    def fill(self, dc, color, left, top, right, bottom):
        brush = self.api.CreateSolidBrush(color)
        if brush:
            try:
                self.api.FillRect(dc, byref(win.RECT(left, top, right, bottom)), brush)
            finally:
                self.api.DeleteObject(brush)

    def label(self, dc, value, color, size, weight, left, top, right, bottom, *, align=0):
        if not value or right <= left:
            return
        api = self.api
        font = api.CreateFontW(-_scale(size, self.dpi), 0, 0, 0, weight,
                               0, 0, 0, 1, 0, 0, 5, 0, "Georgia")
        old = api.SelectObject(dc, font) if font else None
        try:
            api.SetBkMode(dc, 1)
            api.SetTextColor(dc, color)
            api.DrawTextW(dc, value, -1, byref(win.RECT(left, top, right, bottom)), DRAW_TEXT | align)
        finally:
            if old and old != GDI_ERROR:
                api.SelectObject(dc, old)
            if font:
                api.DeleteObject(font)

    def draw(self, dc, width, height):
        view = self.view
        dpi = self.dpi
        pad = _scale(10, dpi)
        self.fill(dc, _color(25, 22, 17), 0, 0, width, height)
        self.fill(dc, _color(82, 70, 55), 0, 0, width, 1)
        gold, ivory = _color(247, 206, 102), _color(243, 235, 216)
        x, y = _scale(18, dpi), _scale(13 if view.minimized else 18, dpi)
        if view.working:
            radius, dot = _scale(6, dpi), max(2, _scale(2, dpi))
            phase = int(monotonic() / TICK_SECONDS)
            for index, (cosine, sine) in enumerate(SPINNER):
                intensity = 79 + 19 * ((index - phase) % len(SPINNER))
                left, top = x + round(radius * cosine), y + round(radius * sine)
                self.fill(dc, _color(intensity, intensity, intensity), left, top, left + dot, top + dot)
        else:
            dot = _scale(4, dpi)
            self.fill(dc, gold, x, y, x + dot, y + dot)

        leading = _scale(30, dpi)
        if view.minimized:
            secondary_text = view.reload_hint or view.occurrence
            middle = _scale(24, dpi) if secondary_text else height
            self.label(dc, view.title or view.count or view.detail, gold, 12, 500,
                       leading, 0, width - pad, middle)
            self.label(dc, secondary_text, ivory, 11, 500,
                       leading, middle, width - pad, height)
            return
        count_left = width - min(_scale(112, dpi), width * 2 // 5)
        title_right = count_left - pad if view.count else width - pad
        detail_right = count_left - pad if view.occurrence else width - pad
        self.label(dc, view.title, gold, 15, 600, leading, _scale(6, dpi), title_right,
                   _scale(27, dpi))
        self.label(dc, view.count, ivory, 13, 500, count_left, _scale(6, dpi), width - pad,
                   _scale(27, dpi), align=2)
        self.label(dc, view.detail, ivory, 13, 400, pad, _scale(28, dpi), detail_right,
                   _scale(45, dpi))
        self.label(dc, view.occurrence, ivory, 13, 500, count_left, _scale(28, dpi), width - pad,
                   _scale(45, dpi), align=2)
        if view.reload_hint:
            self.label(dc, view.reload_hint, _color(233, 202, 141), 11, 600,
                       pad, _scale(45, dpi), width - pad, _scale(62, dpi))
        track_top = height - _scale(11, dpi)
        track_bottom = track_top + max(2, _scale(4, dpi))
        self.fill(dc, _color(72, 65, 54), pad, track_top, width - pad, track_bottom)
        if view.percent:
            edge = pad + round((width - 2 * pad) * view.percent / 100)
            self.fill(dc, gold, pad, track_top, edge, track_bottom)


def start(wow_root, sample):
    """Start the display and return its close callback, or None if unavailable."""
    if sys.platform != "win32":
        return None
    try:
        game_exe = Path(wow_root) / "Wow.exe"
        if not game_exe.is_file():
            return None
        popup = _Popup(_image_path(game_exe), sample)
        popup.thread.start()
    except (OSError, RuntimeError, TypeError, ValueError):
        LOG.warning("The Windows progress display could not start.", exc_info=True)
        return None
    if popup.ready.wait(timeout=2) and popup.hwnd and not popup.stop.is_set():
        def close():
            popup.close()

        close.is_alive = lambda: popup.thread.is_alive() and not popup.stop.is_set()
        return close
    popup.close()
    return None
