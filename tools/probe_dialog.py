"""Probe foreground window to find a file dialog's current folder.

Run from a terminal:
    py tools/probe_dialog.py

You have 6 seconds to switch focus to a file Open/Save dialog. The
probe then:
  1. Walks the UIA tree of that window
  2. Finds the breadcrumb toolbar (name starts with "Address: ")
  3. Resolves the display path to a real filesystem path via
     SHParseDisplayName + SHGetPathFromIDList.
"""
import ctypes
import sys
import time
from ctypes import POINTER, byref, c_ulong, c_void_p, c_wchar_p, wintypes

try:
    import uiautomation as auto
except ImportError:
    print("Installing uiautomation...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "uiautomation"])
    import uiautomation as auto

import win32gui


shell32 = ctypes.windll.shell32
ole32 = ctypes.windll.ole32

SHParseDisplayName = shell32.SHParseDisplayName
SHParseDisplayName.argtypes = [c_wchar_p, c_void_p, POINTER(c_void_p), c_ulong, POINTER(c_ulong)]
SHParseDisplayName.restype = ctypes.HRESULT

SHGetPathFromIDListW = shell32.SHGetPathFromIDListW
SHGetPathFromIDListW.argtypes = [c_void_p, c_wchar_p]
SHGetPathFromIDListW.restype = wintypes.BOOL

CoTaskMemFree = ole32.CoTaskMemFree
CoTaskMemFree.argtypes = [c_void_p]


def display_to_path(display_name: str):
    """'Desktop\\GrappleHook' -> 'C:\\Users\\foo\\Desktop\\GrappleHook' (or None)."""
    pidl = c_void_p()
    sfgao = c_ulong(0)
    try:
        SHParseDisplayName(display_name, None, byref(pidl), 0, byref(sfgao))
    except OSError as e:
        return None, f"SHParseDisplayName error: {e}"
    if not pidl.value:
        return None, "SHParseDisplayName returned null pidl"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        ok = SHGetPathFromIDListW(pidl, buf)
        if ok:
            return buf.value, None
        return None, "SHGetPathFromIDList returned False (probably a virtual folder)"
    finally:
        CoTaskMemFree(pidl)


def find_address_toolbar(root):
    """Find the breadcrumb toolbar whose Name starts with 'Address: '."""
    hits = []

    def walk(ctrl, depth=0):
        if depth > 12:
            return
        try:
            name = ctrl.Name or ""
            if ctrl.ControlTypeName == "ToolBarControl" and name.startswith("Address: "):
                hits.append(ctrl)
            for c in ctrl.GetChildren():
                walk(c, depth + 1)
        except Exception:
            pass

    walk(root)
    return hits


def enum_top_windows():
    """Every visible top-level window with a non-empty title."""
    hits = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            return
        if title:
            hits.append((hwnd, title, cls))

    win32gui.EnumWindows(cb, None)
    return hits


def main():
    print("Open the file dialog and leave it visible. Probing in 3s...", flush=True)
    time.sleep(3)

    wins = enum_top_windows()
    print(f"\n{len(wins)} visible top-level windows. Likely dialogs first:")
    # Heuristic: title contains 'Open' / 'Save' / 'Browse', or class is #32770
    def score(w):
        hwnd, title, cls = w
        s = 0
        t = title.lower()
        if cls == "#32770": s += 10
        for kw in ("open", "save", "browse", "import", "export", "load"):
            if kw in t: s += 5
        return -s

    wins.sort(key=score)
    for hwnd, title, cls in wins[:20]:
        print(f"  hwnd={hwnd}  class={cls!r}  title={title!r}")

    targets = [w for w in wins if score(w) < 0]
    if not targets:
        print("\nNo promising dialog-like windows found.")
        return

    for hwnd, title, cls in targets[:5]:
        print(f"\n--- inspecting hwnd={hwnd} class={cls!r} title={title!r} ---")
        win = auto.ControlFromHandle(hwnd)
        if not win:
            print("  UIA: no control")
            continue
        bars = find_address_toolbar(win)
        if not bars:
            print("  no 'Address: ...' toolbar inside — dumping full UIA tree:")
            def dump(ctrl, depth=0):
                if depth > 14:
                    return
                try:
                    name = (ctrl.Name or "")[:60]
                    cls = (ctrl.ClassName or "")[:40]
                    aid = (ctrl.AutomationId or "")[:40]
                    ct = ctrl.ControlTypeName
                    print(f"  {'  ' * depth}{ct} name={name!r} class={cls!r} aid={aid!r}")
                    for c in ctrl.GetChildren():
                        dump(c, depth + 1)
                except Exception as e:
                    print(f"  {'  ' * depth}[err {e}]")
            dump(win)
            continue
        for bar in bars:
            raw = bar.Name
            display = raw[len("Address: "):]
            print(f"  raw breadcrumb name: {raw!r}")
            print(f"  display path        : {display!r}")
            path, err = display_to_path(display)
            if path:
                print(f"  resolved filesystem : {path}")
            else:
                print(f"  resolution failed   : {err}")


if __name__ == "__main__":
    main()
