#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Window & Region Deferred-Preview Optimization Helpers.
Implements outline-first preview ("show target region first, commit graphics on release")
for Win32 OS window move/resize (Tkinter Studio & Pygame Player) and Tkinter PanedWindows.
"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from typing import Any, Callable, Dict, Optional, Tuple

RectTuple = Tuple[int, int, int, int]

# Unified gray pattern palette across all region/window preview modules
PREVIEW_GRAY_LIGHT_RGB: Tuple[int, int, int] = (184, 188, 196)
PREVIEW_GRAY_DARK_RGB: Tuple[int, int, int] = (88, 92, 100)
PREVIEW_GRAY_LIGHT_HEX = "#B8BCC4"
PREVIEW_GRAY_DARK_HEX = "#585C64"
PREVIEW_GRAY_SASH_HEX = "#D4D8E0"

# Win32 constants
GA_ROOT = 2
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_NCDESTROY = 0x0082
WM_SIZING = 0x0214
WM_MOVING = 0x0216
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
VK_ESCAPE = 0x1B

GWLP_WNDPROC = -4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SW_HIDE = 0
SW_SHOWNA = 8
RGN_DIFF = 4

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

_LEFT_EDGES = frozenset({1, 4, 7})
_RIGHT_EDGES = frozenset({2, 5, 8})
_TOP_EDGES = frozenset({3, 4, 5})
_BOTTOM_EDGES = frozenset({6, 7, 8})


def compute_moving_rect(start_rect: RectTuple, dx: int, dy: int) -> RectTuple:
    """Computes target window rect for an outline move operation."""
    l, t, r, b = start_rect
    return (int(l + dx), int(t + dy), int(r + dx), int(b + dy))


def compute_sizing_rect(
    start_rect: RectTuple,
    edge: int,
    dx: int,
    dy: int,
    min_w: int = 320,
    min_h: int = 240,
) -> RectTuple:
    """Computes clamped target window rect for an 8-edge Win32 WM_SIZING preview."""
    l, t, r, b = start_rect
    mw = max(64, int(min_w))
    mh = max(64, int(min_h))
    if edge in _LEFT_EDGES:
        l = min(r - mw, l + int(dx))
    elif edge in _RIGHT_EDGES:
        r = max(l + mw, r + int(dx))
    if edge in _TOP_EDGES:
        t = min(b - mh, t + int(dy))
    elif edge in _BOTTOM_EDGES:
        b = max(t + mh, b + int(dy))
    return (int(l), int(t), int(r), int(b))


class PreviewPanedWindow(tk.PanedWindow):
    """Non-opaque tk.PanedWindow that renders a preview sash during drag and commits on release."""

    def add(self, child: tk.Misc, **kw: Any) -> None:
        weight = kw.pop("weight", None)
        if weight is not None and "stretch" not in kw:
            kw["stretch"] = "always" if int(weight) >= 2 else "last"
        kw.setdefault("minsize", 80)
        super().add(child, **kw)


def create_preview_paned(parent: tk.Misc, orient: str = "horizontal") -> PreviewPanedWindow:
    """Creates a PanedWindow with opaqueresize=False and unified light-gray sash styling."""
    return PreviewPanedWindow(
        parent,
        orient=orient,
        opaqueresize=False,
        sashwidth=5,
        sashpad=1,
        sashrelief="flat",
        bg=PREVIEW_GRAY_SASH_HEX,
        bd=0,
        relief="flat",
        showhandle=False,
    )


_ACTIVE_CONTROLLERS: Dict[int, "WindowOutlineDragController"] = {}
_PREVIEW_CLASS_ATOM: Optional[int] = None
_PREVIEW_WNDPROC_REF: Optional[Any] = None
_PREVIEW_PATTERN_BRUSH: Optional[Any] = None


def _setup_win32() -> Optional[Tuple[Any, Any, Any, Any]]:
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32
    lresult_t = ctypes.c_ssize_t

    if not hasattr(_setup_win32, "_configured"):
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.IsZoomed.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
        user32.SetWindowRgn.restype = ctypes.c_int
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
        user32.FillRect.restype = ctypes.c_int
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = lresult_t
        user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.CallWindowProcW.restype = lresult_t
        if hasattr(user32, "SetWindowLongPtrW"):
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        else:
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.SetWindowLongW.restype = ctypes.c_void_p

        gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
        gdi32.CreateBitmap.restype = wintypes.HBITMAP
        gdi32.CreatePatternBrush.argtypes = [wintypes.HBITMAP]
        gdi32.CreatePatternBrush.restype = wintypes.HBRUSH
        gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
        gdi32.SetTextColor.restype = wintypes.DWORD
        gdi32.SetBkColor.argtypes = [wintypes.HDC, wintypes.DWORD]
        gdi32.SetBkColor.restype = wintypes.DWORD
        gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        gdi32.CreateRectRgn.restype = wintypes.HRGN
        gdi32.CombineRgn.argtypes = [wintypes.HRGN, wintypes.HRGN, wintypes.HRGN, ctypes.c_int]
        gdi32.CombineRgn.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        _setup_win32._configured = True  # type: ignore[attr-defined]

    return user32, gdi32, kernel32, lresult_t


def _rgb_to_colorref(rgb: Tuple[int, int, int]) -> int:
    return (int(rgb[2]) << 16) | (int(rgb[1]) << 8) | int(rgb[0])


def _ensure_preview_class(user32: Any, gdi32: Any, kernel32: Any, lresult_t: Any) -> Optional[str]:
    global _PREVIEW_CLASS_ATOM, _PREVIEW_WNDPROC_REF, _PREVIEW_PATTERN_BRUSH
    from ctypes import wintypes

    cls_name = "RPGC_OutlinePreviewFrame"
    if _PREVIEW_CLASS_ATOM is not None:
        return cls_name

    class _PAINTSTRUCT(ctypes.Structure):
        _fields_ = [
            ("hdc", wintypes.HDC),
            ("fErase", wintypes.BOOL),
            ("rcPaint", wintypes.RECT),
            ("fRestore", wintypes.BOOL),
            ("fIncUpdate", wintypes.BOOL),
            ("rgbReserved", ctypes.c_byte * 32),
        ]

    user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_PAINTSTRUCT)]
    user32.BeginPaint.restype = wintypes.HDC
    user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_PAINTSTRUCT)]
    user32.EndPaint.restype = wintypes.BOOL

    # 8x8 2px-friendly checkerboard halftone pattern bitmap (word-aligned rows)
    pat_bits = (ctypes.c_uint16 * 8)(0x00CC, 0x00CC, 0x0033, 0x0033, 0x00CC, 0x00CC, 0x0033, 0x0033)
    hbmp = gdi32.CreateBitmap(8, 8, 1, 1, ctypes.byref(pat_bits))
    pat_brush = gdi32.CreatePatternBrush(hbmp)
    if hbmp:
        gdi32.DeleteObject(hbmp)
    if not pat_brush:
        pat_brush = gdi32.CreateSolidBrush(_rgb_to_colorref(PREVIEW_GRAY_DARK_RGB))
    _PREVIEW_PATTERN_BRUSH = pat_brush
    fg_cr = _rgb_to_colorref(PREVIEW_GRAY_LIGHT_RGB)
    bg_cr = _rgb_to_colorref(PREVIEW_GRAY_DARK_RGB)

    def _fill_pattern_rect(hdc: Any, rect: Any) -> None:
        if hdc and pat_brush:
            gdi32.SetTextColor(hdc, fg_cr)
            gdi32.SetBkColor(hdc, bg_cr)
            user32.FillRect(hdc, ctypes.byref(rect), pat_brush)

    wndproc_type = ctypes.WINFUNCTYPE(lresult_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    def _def_proc(h: Any, m: int, w: int, l: int) -> int:
        if m == WM_ERASEBKGND and w:
            rc = wintypes.RECT()
            if user32.GetClientRect(h, ctypes.byref(rc)):
                _fill_pattern_rect(wintypes.HDC(w), rc)
                return 1
        elif m == WM_PAINT:
            ps = _PAINTSTRUCT()
            hdc = user32.BeginPaint(h, ctypes.byref(ps))
            rc = wintypes.RECT()
            if hdc and user32.GetClientRect(h, ctypes.byref(rc)):
                _fill_pattern_rect(hdc, rc)
            user32.EndPaint(h, ctypes.byref(ps))
            return 0
        return int(user32.DefWindowProcW(h, m, w, l))

    _PREVIEW_WNDPROC_REF = wndproc_type(_def_proc)

    class _WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    hinst = kernel32.GetModuleHandleW(None)
    wc = _WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
    wc.lpfnWndProc = ctypes.cast(_PREVIEW_WNDPROC_REF, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.hbrBackground = pat_brush
    wc.lpszClassName = cls_name
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.ATOM
    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if atom:
        _PREVIEW_CLASS_ATOM = int(atom)
        return cls_name
    return None


class WindowOutlineDragController:
    """Win32 per-window subclass that renders a 3px outline preview during drag and commits on drop."""

    def __init__(
        self,
        hwnd: int,
        min_size: Tuple[int, int] = (320, 240),
        on_commit: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        apis = _setup_win32()
        if apis is None:
            raise RuntimeError("Win32 only")
        self.user32, self.gdi32, self.kernel32, self.lresult_t = apis
        from ctypes import wintypes

        self.wintypes = wintypes
        self.hwnd = int(hwnd)
        self.min_w = max(64, int(min_size[0]))
        self.min_h = max(64, int(min_size[1]))
        self.on_commit = on_commit
        self.dragging = False
        self.start_rect: RectTuple = (0, 0, 0, 0)
        self.pending_rect: RectTuple = (0, 0, 0, 0)
        self.start_cursor: Optional[Tuple[int, int]] = None
        self.preview_hwnd: Optional[int] = None
        self._old_wndproc: Optional[int] = None

        wndproc_type = ctypes.WINFUNCTYPE(
            self.lresult_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )
        self._wndproc_cb = wndproc_type(self._wndproc)
        new_ptr = ctypes.cast(self._wndproc_cb, ctypes.c_void_p)
        set_fn = getattr(self.user32, "SetWindowLongPtrW", None) or self.user32.SetWindowLongW
        old_ptr = set_fn(self.hwnd, GWLP_WNDPROC, new_ptr)
        self._old_wndproc = int(old_ptr) if old_ptr else None

    def _show_preview(self, rect: RectTuple) -> None:
        l, t, r, b = rect
        w = max(8, r - l)
        h = max(8, b - t)
        if not self.preview_hwnd:
            cls_name = _ensure_preview_class(self.user32, self.gdi32, self.kernel32, self.lresult_t)
            if not cls_name:
                return
            hinst = self.kernel32.GetModuleHandleW(None)
            self.preview_hwnd = self.user32.CreateWindowExW(
                WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE,
                cls_name,
                "",
                WS_POPUP,
                l,
                t,
                w,
                h,
                None,
                None,
                hinst,
                None,
            )
        if not self.preview_hwnd:
            return
        border = 4
        outer = self.gdi32.CreateRectRgn(0, 0, w, h)
        if w > border * 2 and h > border * 2:
            inner = self.gdi32.CreateRectRgn(border, border, w - border, h - border)
            self.gdi32.CombineRgn(outer, outer, inner, RGN_DIFF)
            self.gdi32.DeleteObject(inner)
        self.user32.SetWindowRgn(self.preview_hwnd, outer, True)
        self.user32.SetWindowPos(
            self.preview_hwnd,
            self.wintypes.HWND(-1),  # HWND_TOPMOST
            l,
            t,
            w,
            h,
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        self.user32.ShowWindow(self.preview_hwnd, SW_SHOWNA)

    def _hide_preview(self) -> None:
        if self.preview_hwnd:
            self.user32.ShowWindow(self.preview_hwnd, SW_HIDE)

    def _destroy_preview(self) -> None:
        if self.preview_hwnd:
            try:
                self.user32.DestroyWindow(self.preview_hwnd)
            except Exception:
                pass
            self.preview_hwnd = None

    def detach(self) -> None:
        self._destroy_preview()
        if self._old_wndproc:
            set_fn = getattr(self.user32, "SetWindowLongPtrW", None) or self.user32.SetWindowLongW
            try:
                set_fn(self.hwnd, GWLP_WNDPROC, ctypes.c_void_p(self._old_wndproc))
            except Exception:
                pass
            self._old_wndproc = None
        _ACTIVE_CONTROLLERS.pop(self.hwnd, None)

    def _read_rect_lparam(self, lparam: int) -> RectTuple:
        rc = ctypes.cast(lparam, ctypes.POINTER(self.wintypes.RECT)).contents
        return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))

    def _write_rect_lparam(self, lparam: int, rect: RectTuple) -> None:
        rc = ctypes.cast(lparam, ctypes.POINTER(self.wintypes.RECT)).contents
        rc.left, rc.top, rc.right, rc.bottom = rect

    def _compute_drag_cand(self, msg: int, wparam: int, lparam: int) -> RectTuple:
        if self.start_cursor is not None:
            pt = self.wintypes.POINT()
            if self.user32.GetCursorPos(ctypes.byref(pt)):
                dx = int(pt.x) - self.start_cursor[0]
                dy = int(pt.y) - self.start_cursor[1]
                if dx or dy:
                    if msg == WM_MOVING:
                        return compute_moving_rect(self.start_rect, dx, dy)
                    return compute_sizing_rect(self.start_rect, int(wparam), dx, dy, self.min_w, self.min_h)
        return self._read_rect_lparam(lparam)

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_ENTERSIZEMOVE:
            if not self.user32.IsZoomed(hwnd) and not self.user32.IsIconic(hwnd):
                rc = self.wintypes.RECT()
                if self.user32.GetWindowRect(hwnd, ctypes.byref(rc)):
                    self.start_rect = (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))
                    self.pending_rect = self.start_rect
                    pt = self.wintypes.POINT()
                    self.start_cursor = (int(pt.x), int(pt.y)) if self.user32.GetCursorPos(ctypes.byref(pt)) else None
                    self.dragging = True
        elif msg in (WM_MOVING, WM_SIZING) and self.dragging and lparam:
            cand = self._compute_drag_cand(msg, wparam, lparam)
            if cand != self.start_rect:
                self.pending_rect = cand
                self._show_preview(cand)
            self._write_rect_lparam(lparam, self.start_rect)
            return 1
        elif msg == WM_EXITSIZEMOVE and self.dragging:
            self.dragging = False
            self._hide_preview()
            canceled = bool(self.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
            target = self.pending_rect
            if not canceled and target != self.start_rect:
                l, t, r, b = target
                nw = max(self.min_w, r - l)
                nh = max(self.min_h, b - t)
                sw = self.start_rect[2] - self.start_rect[0]
                sh = self.start_rect[3] - self.start_rect[1]
                flags = SWP_NOZORDER | SWP_NOACTIVATE
                if (nw, nh) == (sw, sh):
                    flags |= SWP_NOSIZE
                self.user32.SetWindowPos(hwnd, None, l, t, nw, nh, flags)
                if self.on_commit is not None:
                    try:
                        self.on_commit(nw, nh)
                    except Exception:
                        pass
        elif msg == WM_NCDESTROY:
            old_proc = self._old_wndproc
            self.detach()
            if old_proc:
                return int(self.user32.CallWindowProcW(ctypes.c_void_p(old_proc), hwnd, msg, wparam, lparam))
            return int(self.user32.DefWindowProcW(hwnd, msg, wparam, lparam))

        if self._old_wndproc:
            return int(self.user32.CallWindowProcW(ctypes.c_void_p(self._old_wndproc), hwnd, msg, wparam, lparam))
        return int(self.user32.DefWindowProcW(hwnd, msg, wparam, lparam))


def attach_outline_drag_preview(
    raw_hwnd: Any,
    min_size: Tuple[int, int] = (320, 240),
    on_commit: Optional[Callable[[int, int], None]] = None,
) -> Optional[WindowOutlineDragController]:
    """Attaches Win32 outline-preview drag/resize controller to the top-level window HWND."""
    if sys.platform != "win32" or not raw_hwnd:
        return None
    try:
        apis = _setup_win32()
        if apis is None:
            return None
        user32, _, _, _ = apis
        hwnd_int = int(raw_hwnd)
        top_hwnd = int(user32.GetAncestor(hwnd_int, GA_ROOT) or hwnd_int)
        if not top_hwnd or not user32.IsWindow(top_hwnd):
            return None
        if top_hwnd in _ACTIVE_CONTROLLERS:
            return _ACTIVE_CONTROLLERS.get(top_hwnd)
        ctrl = WindowOutlineDragController(top_hwnd, min_size=min_size, on_commit=on_commit)
        _ACTIVE_CONTROLLERS[top_hwnd] = ctrl
        return ctrl
    except Exception:
        return None


def reset_text_with_content(widget: Any, content: str) -> None:
    """Replaces Text widget contents cleanly and clears undo/modified history so Ctrl+Z never wipes loaded text."""
    try:
        prev_auto = bool(widget.cget("autoseparators"))
    except Exception:
        prev_auto = True
    try:
        widget.configure(autoseparators=False)
    except Exception:
        pass
    try:
        widget.delete("1.0", "end")
        if content:
            widget.insert("1.0", content)
        if hasattr(widget, "edit_reset"):
            widget.edit_reset()
        if hasattr(widget, "edit_modified"):
            widget.edit_modified(False)
    finally:
        try:
            widget.configure(autoseparators=prev_auto)
        except Exception:
            pass


def replace_text_undoable(widget: Any, content: str) -> None:
    """Replaces Text widget contents as one atomic undo step so Ctrl+Z restores the previous text cleanly."""
    try:
        prev_auto = bool(widget.cget("autoseparators"))
    except Exception:
        prev_auto = True
    if hasattr(widget, "edit_separator"):
        widget.edit_separator()
    try:
        widget.configure(autoseparators=False)
    except Exception:
        pass
    try:
        widget.delete("1.0", "end")
        if content:
            widget.insert("1.0", content)
        if hasattr(widget, "edit_separator"):
            widget.edit_separator()
    finally:
        try:
            widget.configure(autoseparators=prev_auto)
        except Exception:
            pass


def bind_text_modified(widget: Any, on_dirty: Callable[[Any], None]) -> None:
    """Binds <<Modified>> on a Text widget to trigger on_dirty without blocking repeated notifications."""
    def _handle(_ev: Any = None) -> None:
        try:
            if widget.edit_modified():
                widget.edit_modified(False)
                on_dirty(widget)
        except Exception:
            pass

    widget.bind("<<Modified>>", _handle, add="+")
