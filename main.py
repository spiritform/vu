import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

try:
    from send2trash import send2trash as _send2trash
except Exception:
    _send2trash = None

# PyInstaller --noconsole: stdout/stderr are None, which breaks any print/log call
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PIL import Image

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
STATIC_DIR = APP_DIR / "static"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".m4v"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

THUMB_SIZE = 400


def _app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    d = base / "VU"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _folder_id(root: Path) -> str:
    return hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:16]


DEFAULT_HOTKEY = "ctrl+shift+v"


def _settings_path() -> Path:
    return _app_data_dir() / "settings.json"


def _load_settings() -> dict:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_settings(data: dict) -> None:
    _settings_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def _settings_with_defaults() -> dict:
    s = _load_settings()
    s.setdefault("hotkey", DEFAULT_HOTKEY)
    return s

# Hide subprocess console windows on Windows
_SUBPROCESS_KWARGS = {}
if os.name == "nt":
    _SUBPROCESS_KWARGS["creationflags"] = 0x08000000  # CREATE_NO_WINDOW


def _ffmpeg_path() -> str:
    """Return the bundled ffmpeg if present, otherwise fall back to PATH."""
    if sys.platform == "win32":
        cand = APP_DIR / "assets" / "ffmpeg" / "win" / "ffmpeg.exe"
    elif sys.platform == "darwin":
        cand = APP_DIR / "assets" / "ffmpeg" / "mac" / "ffmpeg"
    else:
        cand = None
    if cand and cand.exists():
        return str(cand)
    return "ffmpeg"

app = FastAPI()

# Session state — single folder at a time (local tool, single user)
state = {"root": None}

# Set by the main block once the Qt window exists; lets /api/show forward
# "bring me forward" pings from a second-instance launch to the live window.
_on_show_request = None


def _runtime_file() -> Path:
    return _app_data_dir() / "runtime.json"


@app.get("/api/ping")
def ping():
    return {"ok": True, "pid": os.getpid()}


@app.post("/api/show")
def show_existing():
    if _on_show_request:
        _on_show_request()
    return {"ok": True}


# Set by the main block once the hotkey path knows how to re-register itself.
_re_register_hotkey = None


class SettingsBody(BaseModel):
    hotkey: str


@app.get("/api/settings")
def get_settings():
    return _settings_with_defaults()


@app.post("/api/settings")
def post_settings(body: SettingsBody):
    hk = (body.hotkey or "").strip().lower()
    if not hk:
        raise HTTPException(400, "hotkey required")
    # Try to bind it before persisting — a bad combo (unknown key name,
    # already-held shortcut) shouldn't survive to the next launch and lock
    # the user out of the global hotkey.
    if _re_register_hotkey and not _re_register_hotkey(hk):
        raise HTTPException(400, f"could not register hotkey '{hk}'")
    s = _settings_with_defaults()
    s["hotkey"] = hk
    _save_settings(s)
    return s


def _hearts_path(root: Path) -> Path:
    d = _app_data_dir() / "hearts"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_folder_id(root)}.json"


def load_hearts(root: Path) -> set[str]:
    p = _hearts_path(root)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("hearts", data) if isinstance(data, dict) else data)
    except Exception:
        return set()


def save_hearts(root: Path, hearts: set[str]) -> None:
    p = _hearts_path(root)
    payload = {"root": str(root), "hearts": sorted(hearts)}
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def thumb_file(root: Path, rel: str, mtime: float, size: int) -> Path:
    key = f"{rel}|{int(mtime)}|{size}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    d = _app_data_dir() / "thumbs" / _folder_id(root)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.jpg"


def make_image_thumb(src: Path, dst: Path) -> bool:
    try:
        with Image.open(src) as img:
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            img.save(dst, "JPEG", quality=82, optimize=True)
        return True
    except Exception as e:
        print(f"[thumb] image fail {src.name}: {e}", file=sys.stderr)
        return False


def make_video_thumb(src: Path, dst: Path) -> bool:
    cmd_base = [
        _ffmpeg_path(), "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-frames:v", "1",
        "-vf", f"scale={THUMB_SIZE}:-1:force_original_aspect_ratio=decrease",
        "-q:v", "4", "-y", str(dst),
    ]
    # Try seeking a bit in for a more interesting frame; fall back to frame 0
    for ss in ("00:00:01", None):
        cmd = list(cmd_base)
        if ss:
            cmd = cmd[:3] + ["-ss", ss] + cmd[3:]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30, **_SUBPROCESS_KWARGS)
            if dst.exists() and dst.stat().st_size > 0:
                return True
        except FileNotFoundError:
            print("[thumb] ffmpeg not found on PATH", file=sys.stderr)
            return False
        except Exception:
            pass
    return False


def validate_path(rel: str) -> Path:
    root = state["root"]
    if not root:
        raise HTTPException(400, "No folder scanned yet")
    full = (root / rel).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        raise HTTPException(403, "Path outside scanned folder")
    return full


class ScanBody(BaseModel):
    folder: str
    recursive: bool = False


class PathBody(BaseModel):
    path: str


class HeartBody(BaseModel):
    path: str
    hearted: bool


class ExportBody(BaseModel):
    subfolder: str = "selects"
    move: bool = False
    zip: bool = False
    paths: list[str] | None = None  # if provided, export these; else export hearts


@app.post("/api/scan")
def scan(body: ScanBody):
    root = Path(body.folder).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(400, f"Not a directory: {root}")
    state["root"] = root

    iterator = root.rglob("*") if body.recursive else root.iterdir()
    hearts = load_hearts(root)

    items = []
    seen_rels: set[str] = set()
    for p in iterator:
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in MEDIA_EXTS:
            continue
        rel = p.relative_to(root).as_posix()
        if rel in seen_rels:
            # Defensive: junctions/symlinks under recursive scan can reach the
            # same file via different paths and produce visible duplicates.
            continue
        seen_rels.add(rel)
        stat = p.stat()
        items.append({
            "path": rel,
            "name": p.name,
            "kind": "video" if ext in VIDEO_EXTS else "image",
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "hearted": rel in hearts,
        })

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"root": str(root), "count": len(items), "items": items}


class DropBody(BaseModel):
    paths: list[str]


@app.post("/api/drop")
def drop(body: DropBody):
    if not body.paths:
        raise HTTPException(400, "no paths")
    first = Path(body.paths[0])
    if first.is_dir():
        return {"folder": str(first.resolve()), "is_dir": True}
    if first.exists():
        return {"folder": str(first.parent.resolve()), "is_dir": False}
    raise HTTPException(400, "path not found")


def _unique_target(dest_dir: Path, name: str) -> Path:
    # Sanitize: strip path separators, control chars, illegal Windows chars
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().strip(".") or "import"
    p = dest_dir / safe
    if not p.exists():
        return p
    stem, suffix = Path(safe).stem, Path(safe).suffix
    i = 1
    while True:
        cand = dest_dir / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


class ImportPathsBody(BaseModel):
    paths: list[str]


@app.post("/api/import_paths")
def import_paths(body: ImportPathsBody):
    root = state["root"]
    if not root:
        raise HTTPException(400, "Scan a folder first")
    if not body.paths:
        raise HTTPException(400, "no paths")
    dest = Path(root)
    copied = 0
    skipped = 0
    for src_str in body.paths:
        src = Path(src_str)
        if not src.exists() or not src.is_file():
            skipped += 1
            continue
        if src.suffix.lower() not in MEDIA_EXTS:
            skipped += 1
            continue
        try:
            if src.resolve().parent == dest.resolve():
                # already lives here — nothing to do
                continue
        except Exception:
            pass
        target = _unique_target(dest, src.name)
        try:
            shutil.copy2(str(src), str(target))
            copied += 1
        except Exception as e:
            print(f"[import] copy fail {src}: {e}", file=sys.stderr)
            skipped += 1
    return {"ok": True, "copied": copied, "skipped": skipped, "folder": str(dest)}


_CT_TO_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg",
    "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/avif": ".avif", "image/bmp": ".bmp",
    "video/mp4": ".mp4", "video/webm": ".webm",
    "video/quicktime": ".mov", "video/x-matroska": ".mkv",
}


@app.post("/api/import_blob")
async def import_blob(request: Request):
    """Save a raw clipboard image (paste from browser, etc.) into the open folder."""
    root = state["root"]
    if not root:
        raise HTTPException(400, "Scan a folder first")
    ct = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    ext = _CT_TO_EXT.get(ct)
    if not ext:
        raise HTTPException(400, f"Unsupported media type: {ct or 'unknown'}")
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty paste body")
    import time
    name = f"paste_{int(time.time())}{ext}"
    target = _unique_target(Path(root), name)
    target.write_bytes(data)
    return {"ok": True, "filename": target.name, "folder": str(root)}


class ImportUrlBody(BaseModel):
    url: str


@app.post("/api/import_url")
def import_url(body: ImportUrlBody):
    root = state["root"]
    if not root:
        raise HTTPException(400, "Scan a folder first")
    url = (body.url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise HTTPException(400, "Only http(s) URLs are supported")

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; VU/1.0)",
            "Accept": "image/*,video/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            cd = resp.headers.get("Content-Disposition") or ""
            data = resp.read()
    except Exception as e:
        raise HTTPException(400, f"Fetch failed: {e}")

    # Derive a filename: prefer Content-Disposition, fall back to URL path
    name = ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.IGNORECASE)
    if m:
        try:
            name = urllib.parse.unquote(m.group(1))
        except Exception:
            name = m.group(1)
    if not name:
        name = Path(urllib.parse.urlparse(url).path).name
    name = name or "import"

    ext = Path(name).suffix.lower()
    if ext not in MEDIA_EXTS:
        guess = _CT_TO_EXT.get(ct)
        if not guess:
            raise HTTPException(400, f"Unsupported media type: {ct or 'unknown'}")
        # If the filename ended in something irrelevant (e.g. ".aspx"), replace it
        name = (Path(name).stem or "import") + guess

    dest = Path(root)
    target = _unique_target(dest, name)
    target.write_bytes(data)
    return {"ok": True, "filename": target.name, "folder": str(dest)}


@app.get("/api/file")
def get_file(path: str = Query(...)):
    full = validate_path(path)
    if not full.exists():
        raise HTTPException(404, "Not found")
    # inline disposition with filename => browsers preserve name on drag-to-desktop
    return FileResponse(
        full,
        headers={"Content-Disposition": f'inline; filename="{full.name}"'},
    )


@app.get("/api/thumb")
def get_thumb(path: str = Query(...)):
    full = validate_path(path)
    if not full.exists():
        raise HTTPException(404, "Not found")
    root = state["root"]
    stat = full.stat()
    thumb = thumb_file(root, path, stat.st_mtime, stat.st_size)
    if not thumb.exists():
        ext = full.suffix.lower()
        ok = make_video_thumb(full, thumb) if ext in VIDEO_EXTS else make_image_thumb(full, thumb)
        if not ok:
            # Fallback: serve original (so UI still shows something)
            return FileResponse(full)
    return FileResponse(thumb, headers={"Cache-Control": "public, max-age=86400"})


# QtWebEngine's (PyPI) Chromium ships without proprietary codecs, so it can't
# decode H.264/HEVC in the <video> element — only VP8/VP9/AV1/Theora. We
# transcode unsupported videos to VP9/WebM on demand and cache the result so
# they play inside the normal HTML lightbox (with all its chrome) on replay.
WEB_PLAYABLE_VCODECS = {"vp8", "vp9", "av1", "theora"}

# VP9 transcode quality knobs, tuned for visual fidelity. Lower CRF = higher
# quality/larger files; lower cpu-used = slower/better. See _transcode_to_webm.
VIDEO_TRANSCODE_CRF = "23"
VIDEO_TRANSCODE_CPU_USED = "4"

_transcode_master_lock = threading.Lock()
_transcode_locks: dict[str, threading.Lock] = {}


def _video_cache_path(root: Path, rel: str, mtime: float, size: int) -> Path:
    key = f"{rel}|{int(mtime)}|{size}|vp9"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    d = _app_data_dir() / "video_cache" / _folder_id(root)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.webm"


def _probe_video_codec(src: Path) -> str:
    """Return the source's video codec name (lowercase), or '' if unknown."""
    try:
        out = subprocess.run(
            [_ffmpeg_path(), "-hide_banner", "-i", str(src)],
            capture_output=True, text=True, timeout=15, **_SUBPROCESS_KWARGS,
        ).stderr
    except Exception:
        return ""
    m = re.search(r"Stream #\d+:\d+.*: Video: (\w+)", out)
    return m.group(1).lower() if m else ""


def _transcode_to_webm(src: Path, dst: Path) -> bool:
    """Transcode to VP9/WebM, tuned for visual fidelity (CRF 23, full res)."""
    tmp = dst.with_name(dst.stem + ".partial.webm")
    cmd = [
        _ffmpeg_path(), "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-c:v", "libvpx-vp9", "-crf", VIDEO_TRANSCODE_CRF, "-b:v", "0",
        "-deadline", "good", "-cpu-used", VIDEO_TRANSCODE_CPU_USED, "-row-mt", "1",
        "-pix_fmt", "yuv420p",
        "-c:a", "libopus", "-b:a", "128k",
        "-y", str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600, **_SUBPROCESS_KWARGS)
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(dst)   # atomic — never serve a half-written file
            return True
    except Exception as e:
        print(f"[video] transcode failed {src.name}: {e}", file=sys.stderr)
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    return False


@app.get("/api/video")
def get_video(path: str = Query(...)):
    full = validate_path(path)
    if not full.exists():
        raise HTTPException(404, "Not found")
    root = state["root"]
    stat = full.stat()
    cache = _video_cache_path(root, path, stat.st_mtime, stat.st_size)

    if cache.exists():
        return FileResponse(cache, media_type="video/webm",
                            headers={"Cache-Control": "private, max-age=86400"})

    # Already a codec the webview can decode? Serve the original untouched.
    if _probe_video_codec(full) in WEB_PLAYABLE_VCODECS:
        return FileResponse(full)

    # Serialize per cache key so two requests for the same clip don't both encode.
    with _transcode_master_lock:
        lock = _transcode_locks.setdefault(str(cache), threading.Lock())
    with lock:
        if not cache.exists() and not _transcode_to_webm(full, cache):
            raise HTTPException(500, "Video transcode failed")
    return FileResponse(cache, media_type="video/webm",
                        headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/heart")
def heart(body: HeartBody):
    root = state["root"]
    if not root:
        raise HTTPException(400, "No folder scanned")
    validate_path(body.path)
    hearts = load_hearts(root)
    if body.hearted:
        hearts.add(body.path)
    else:
        hearts.discard(body.path)
    save_hearts(root, hearts)
    return {"ok": True, "hearted": body.hearted, "total": len(hearts)}


@app.post("/api/delete")
def delete(body: PathBody):
    root = state["root"]
    if not root:
        raise HTTPException(400, "No folder scanned")
    full = validate_path(body.path)

    # If the file is gone already, still clear the heart and report ok
    if full.exists():
        trashed = False
        if _send2trash is not None:
            try:
                _send2trash(str(full))
                trashed = True
            except Exception as e:
                print(f"[delete] send2trash failed for {full}: {e}", file=sys.stderr)
        if not trashed:
            try:
                full.unlink()
            except Exception as e:
                raise HTTPException(500, f"Delete failed: {e}")

    hearts = load_hearts(root)
    if body.path in hearts:
        hearts.discard(body.path)
        save_hearts(root, hearts)
    return {"ok": True, "trashed": _send2trash is not None}


@app.post("/api/export")
def export(body: ExportBody):
    root = state["root"]
    if not root:
        raise HTTPException(400, "No folder scanned")
    hearts = load_hearts(root)
    if not body.paths and not hearts:
        raise HTTPException(400, "Nothing to export")

    # prefer explicit paths (selection); fall back to hearts
    rels = body.paths if body.paths else sorted(hearts)
    sources = []
    for rel in rels:
        src = (root / rel).resolve()
        try:
            src.relative_to(root)
        except ValueError:
            continue
        if src.exists():
            sources.append(src)

    if body.zip:
        zip_path = root / f"{body.subfolder}.zip"
        i = 1
        while zip_path.exists():
            zip_path = root / f"{body.subfolder}_{i}.zip"
            i += 1
        used_names: set[str] = set()
        count = 0
        # ZIP_STORED: media is already compressed, skip re-compressing
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            for src in sources:
                arc = src.name
                j = 1
                while arc in used_names:
                    arc = f"{src.stem}_{j}{src.suffix}"
                    j += 1
                used_names.add(arc)
                z.write(src, arcname=arc)
                count += 1
        if body.move:
            for src in sources:
                try:
                    src.unlink()
                except Exception:
                    pass
            save_hearts(root, set())
        return {"ok": True, "exported": count, "dest": str(zip_path)}

    # Folder export
    dest = root / body.subfolder
    dest.mkdir(exist_ok=True)
    copied = 0
    for src in sources:
        target = dest / src.name
        i = 1
        while target.exists() and target.resolve() != src.resolve():
            target = dest / f"{src.stem}_{i}{src.suffix}"
            i += 1
        if target.resolve() == src.resolve():
            continue
        if body.move:
            shutil.move(str(src), str(target))
        else:
            shutil.copy2(str(src), str(target))
        copied += 1
    return {"ok": True, "exported": copied, "dest": str(dest)}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def _display_name_to_path(display_name: str):
    """Resolve a shell display name (e.g. 'Desktop\\GrappleHook' or
    'C:\\Users\\foo\\Pictures') to a filesystem path via the shell namespace,
    or return None for virtual folders that have no FS path."""
    import ctypes
    from ctypes import POINTER, byref, c_ulong, c_void_p, c_wchar_p, wintypes

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32

    SHParseDisplayName = shell32.SHParseDisplayName
    SHParseDisplayName.argtypes = [c_wchar_p, c_void_p, POINTER(c_void_p),
                                   c_ulong, POINTER(c_ulong)]
    SHParseDisplayName.restype = ctypes.HRESULT
    SHGetPathFromIDListW = shell32.SHGetPathFromIDListW
    SHGetPathFromIDListW.argtypes = [c_void_p, c_wchar_p]
    SHGetPathFromIDListW.restype = wintypes.BOOL
    CoTaskMemFree = ole32.CoTaskMemFree
    CoTaskMemFree.argtypes = [c_void_p]

    pidl = c_void_p()
    sfgao = c_ulong(0)
    try:
        SHParseDisplayName(display_name, None, byref(pidl), 0, byref(sfgao))
    except OSError:
        return None
    if not pidl.value:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        return buf.value if SHGetPathFromIDListW(pidl, buf) else None
    finally:
        CoTaskMemFree(pidl)


def _get_dialog_folder(hwnd):
    """If hwnd is a Win32 Open/Save dialog (class '#32770'), walk its UIA
    tree for the breadcrumb toolbar (Name starts with 'Address: ') and
    resolve to a filesystem path. Returns None on miss or any failure."""
    try:
        import win32gui
        if win32gui.GetClassName(hwnd) != "#32770":
            return None
        import uiautomation as auto
        root = auto.ControlFromHandle(hwnd)
        if not root:
            return None

        # Bounded BFS — the address toolbar sits ~5 levels deep, and any
        # branch that throws (UIA hiccups) should not abort the whole walk.
        queue = [(root, 0)]
        while queue:
            ctrl, depth = queue.pop(0)
            if depth > 12:
                continue
            try:
                if ctrl.ControlTypeName == "ToolBarControl":
                    name = ctrl.Name or ""
                    if name.startswith("Address: "):
                        path = _display_name_to_path(name[len("Address: "):])
                        if path:
                            return path
            except Exception:
                pass
            try:
                for c in ctrl.GetChildren():
                    queue.append((c, depth + 1))
            except Exception:
                continue
    except Exception:
        return None
    return None


def _get_explorer_context():
    """Return (folder_path, (x, y, w, h)) for the foreground file-manager
    window, or (None, None). The rect lets us reposition VU over it."""
    if IS_WIN:
        try:
            import pythoncom
            import win32gui
            import win32com.client

            pythoncom.CoInitialize()
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None, None
            shell = win32com.client.Dispatch("Shell.Application")
            for w in shell.Windows():
                try:
                    if int(w.HWND) == hwnd:
                        path = w.Document.Folder.Self.Path
                        try:
                            l, t, r, b = win32gui.GetWindowRect(hwnd)
                            rect = (l, t, max(1, r - l), max(1, b - t))
                        except Exception:
                            rect = None
                        return path, rect
                except Exception:
                    continue
            # Fallback: foreground may be an OS file-Open/Save dialog (e.g.
            # Photoshop, Notepad). The shell namespace doesn't expose those,
            # so we ask UIA for the dialog's breadcrumb path.
            path = _get_dialog_folder(hwnd)
            if path:
                return path, None
        except Exception:
            return None, None
        return None, None

    if IS_MAC:
        try:
            # The Finder terms (`Finder window`, `target`) only resolve inside a
            # `tell application "Finder"` block — without it the script fails to
            # COMPILE (-2741), which the inner `try` can't catch (compile happens
            # before it runs), so osascript exits empty and VU opens with no
            # folder. Keep the tell wrapper.
            script = (
                'tell application "Finder"\n'
                '    try\n'
                '        return POSIX path of (target of front Finder window as alias)\n'
                '    on error\n'
                '        return ""\n'
                '    end try\n'
                'end tell'
            )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=2,
            )
            path = result.stdout.strip()
            return (path or None), None
        except Exception:
            return None, None

    return None, None


_FONT_CANDIDATES = [
    "arialbd.ttf", "arial.ttf",              # Windows
    "/System/Library/Fonts/Helvetica.ttc",   # macOS
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/Library/Fonts/Arial.ttf",
    "DejaVuSans-Bold.ttf",                    # Linux
]


def _load_font(size: int):
    from PIL import ImageFont
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _make_tray_icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (64, 64), "black")
    d = ImageDraw.Draw(img)
    font = _load_font(34)
    text = "VU"
    bbox = d.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    d.text(((64 - w) / 2 - bbox[0], (64 - h) / 2 - bbox[1]), text, fill="white", font=font)
    return img


if __name__ == "__main__":
    import socket
    import threading
    import time
    import urllib.request
    import uvicorn
    if IS_WIN:
        # pystray + `keyboard` drive the Windows tray and global hotkey. Both
        # misbehave on macOS:
        #   - keyboard._darwinkeyboard is broken on Apple Silicon: it declares
        #     CFDataGetBytes with an incomplete argtypes then calls it with a
        #     by-value CFRange + output buffer, so the arm64 call is
        #     mis-marshalled and CFDataGetBytes scribbles over memory → SIGBUS.
        #   - pystray's macOS backend runs its own NSApplication loop, which is
        #     illegal off the main thread (Qt already owns it) → AppKit traps.
        # On macOS we use QSystemTrayIcon + an NSEvent monitor instead (below).
        import pystray
        import keyboard

    from PySide6.QtWidgets import QApplication, QMainWindow, QFileDialog
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    from PySide6.QtCore import QUrl, Qt, QEvent, Signal, QTimer
    from PySide6.QtGui import QIcon, QCursor, QGuiApplication

    # Fixed local port held for the lifetime of the process — used as an atomic
    # singleton lock. The actual server runs on a separate port.
    LOCK_PORT = 50447

    def _acquire_lock():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", LOCK_PORT))
            s.listen(1)
            return s
        except OSError:
            s.close()
            return None

    def _forward_then_exit():
        """Another instance owns the lock — ping it (with retries, in case it's
        still booting), tell it to show its window, then exit."""
        rf = _runtime_file()
        deadline = time.time() + 4
        while time.time() < deadline:
            try:
                data = json.loads(rf.read_text(encoding="utf-8"))
                port = int(data.get("port") or 0)
                if port:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/show",
                        data=b"", timeout=0.6,
                    ).read()
                    break
            except Exception:
                pass
            time.sleep(0.25)
        sys.exit(0)

    _lock_socket = _acquire_lock()
    if _lock_socket is None:
        _forward_then_exit()

    def pick_port(preferred: int = 8003) -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", preferred))
            s.close()
            return preferred
        except OSError:
            s.close()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    PORT = pick_port()

    def run_server():
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")

    threading.Thread(target=run_server, daemon=True).start()

    # wait for server to come up
    deadline = time.time() + 5
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.1)
        try:
            s.connect(("127.0.0.1", PORT))
            s.close()
            break
        except Exception:
            time.sleep(0.1)
        finally:
            try:
                s.close()
            except Exception:
                pass

    icon_path = APP_DIR / "assets" / "vu.ico"

    class WebView(QWebEngineView):
        """QWebEngineView subclass that intercepts file drops onto the window."""

        dropped = Signal(list)

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setAcceptDrops(True)
            QTimer.singleShot(0, self._install_filter)

        def _install_filter(self):
            fp = self.focusProxy()
            if fp is not None:
                fp.installEventFilter(self)

        def _has_local_files(self, e) -> bool:
            md = e.mimeData()
            return bool(md and md.hasUrls() and any(u.isLocalFile() for u in md.urls()))

        def _accept_if_files(self, e) -> bool:
            if self._has_local_files(e):
                e.acceptProposedAction()
                return True
            return False

        def _emit_drop(self, e):
            md = e.mimeData()
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if paths:
                self.dropped.emit(paths)
            e.acceptProposedAction()

        def eventFilter(self, obj, e):
            t = e.type()
            if t in (QEvent.DragEnter, QEvent.DragMove):
                if self._accept_if_files(e):
                    return True
            elif t == QEvent.Drop:
                # Only consume drops with local files. Browser drops (URLs only)
                # fall through to the webview so the page can handle them in JS.
                if self._has_local_files(e):
                    self._emit_drop(e)
                    return True
            return super().eventFilter(obj, e)

        def dragEnterEvent(self, e):
            if not self._accept_if_files(e):
                super().dragEnterEvent(e)

        def dragMoveEvent(self, e):
            if not self._accept_if_files(e):
                super().dragMoveEvent(e)

        def dropEvent(self, e):
            if self._has_local_files(e):
                self._emit_drop(e)
            else:
                super().dropEvent(e)

        def contextMenuEvent(self, event):
            # strip navigation actions that don't belong in an embedded UI
            menu = self.createStandardContextMenu()
            page = self.page()
            for action_id in (
                QWebEnginePage.Reload,
                QWebEnginePage.Back,
                QWebEnginePage.Forward,
                QWebEnginePage.Stop,
                QWebEnginePage.ViewSource,
                QWebEnginePage.SavePage,
                QWebEnginePage.OpenLinkInNewTab,
                QWebEnginePage.OpenLinkInNewWindow,
                QWebEnginePage.OpenLinkInNewBackgroundTab,
            ):
                a = page.action(action_id)
                if a is not None:
                    a.setVisible(False)
            menu.exec(event.globalPos())

    class MainWindow(QMainWindow):
        # cross-thread "open me with this folder" signal — empty string means just show.
        # rect (object) is (x, y, w, h) of the Explorer window to align over, or None.
        show_with_folder = Signal(str, object)

        def __init__(self):
            super().__init__()
            # Icon-only title bar. A truly empty title makes Qt fall back to
            # the executable name (`pythonw` from source, `VU` from the bundled
            # build), so use a non-breaking space — visually blank, but Qt
            # treats it as a real title and skips the fallback.
            self.setWindowTitle("\u00A0")
            if icon_path.exists():
                self.setWindowIcon(QIcon(str(icon_path)))
            self.resize(1400, 900)
            self._last_save_dir = None

            self.view = WebView(self)
            # Local trusted content — let videos autoplay. QtWebEngine otherwise
            # blocks programmatic play() without a live user gesture, and ours
            # expires during a server-side transcode, leaving the video paused.
            self.view.settings().setAttribute(
                QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False
            )
            self.view.load(QUrl(f"http://127.0.0.1:{PORT}"))
            self.setCentralWidget(self.view)

            self.view.dropped.connect(self._on_dropped)
            self.show_with_folder.connect(self._handle_show)

            # Wire up downloads so "Save image as..." actually shows a dialog
            self.view.page().profile().downloadRequested.connect(self._on_download)

        def _on_download(self, dl):
            suggested = dl.suggestedFileName() or "download"
            last_dir = self._last_save_dir or str(Path.home() / "Downloads")
            initial = str(Path(last_dir) / suggested)
            file_path, _ = QFileDialog.getSaveFileName(self, "Save As", initial)
            if not file_path:
                dl.cancel()
                return
            self._last_save_dir = os.path.dirname(file_path)
            dl.setDownloadDirectory(os.path.dirname(file_path))
            dl.setDownloadFileName(os.path.basename(file_path))
            dl.accept()

        def closeEvent(self, e):
            # hide to tray instead of quitting
            e.ignore()
            self.hide()

        def _handle_show(self, folder: str, rect):
            # Always open big and centered on the active screen. The Explorer
            # rect (rect) is intentionally ignored — aligning to it puts VU
            # off-screen when the source window is near the top/edge.
            screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            avail = screen.availableGeometry()
            w = min(1400, avail.width() - 80)
            h = min(900, avail.height() - 80)
            x = avail.x() + (avail.width() - w) // 2
            y = avail.y() + (avail.height() - h) // 2
            self.setGeometry(int(x), int(y), int(w), int(h))
            self.show()
            self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
            self.activateWindow()
            self.raise_()
            if IS_WIN:
                # Pin VU on top so drag-into from Explorer/browser works
                # without VU disappearing behind the source window. Setting
                # HWND_TOPMOST also gets us the foreground steal Qt can't
                # reliably do on its own.
                try:
                    import win32gui, win32con
                    hwnd = int(self.winId())
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetWindowPos(
                        hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                    )
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            if folder:
                self.view.page().runJavaScript(
                    f"window.openFolder({json.dumps(folder)})"
                )

        def _on_dropped(self, paths: list):
            # Make sure window is visible when something gets dropped
            if not self.isVisible():
                self._handle_show("", None)
            self.view.page().runJavaScript(
                f"window.openDropped({json.dumps(paths)})"
            )

    qt_app = QApplication.instance() or QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)
    if icon_path.exists():
        qt_app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()  # starts hidden — user invokes via hotkey or tray

    # Let /api/show forward "second-instance launched" pings to the live window
    def _request_show():
        window.show_with_folder.emit("", None)
    globals()["_on_show_request"] = _request_show

    # Publish our port so a future launch can find us
    try:
        _runtime_file().write_text(
            json.dumps({"port": PORT, "pid": os.getpid()}), encoding="utf-8"
        )
    except Exception:
        pass

    def on_hotkey():
        folder, rect = _get_explorer_context()
        window.show_with_folder.emit(folder or "", rect)

    def tray_open(icon, item):
        window.show_with_folder.emit("", None)

    def tray_quit(icon, item):
        try:
            tray.stop()
        except Exception:
            pass
        try:
            _runtime_file().unlink(missing_ok=True)
        except Exception:
            pass
        qt_app.quit()
        if sys.platform == "win32":
            # Kill the whole process tree so QtWebEngine helpers / uvicorn don't linger
            subprocess.Popen(
                ["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                **_SUBPROCESS_KWARGS,
            )
        os._exit(0)

    def _qt_tray_quit(*_):
        try:
            _runtime_file().unlink(missing_ok=True)
        except Exception:
            pass
        qt_app.quit()
        os._exit(0)

    if IS_WIN:
        tray = pystray.Icon(
            "VU",
            _make_tray_icon_image(),
            "VU",
            menu=pystray.Menu(
                pystray.MenuItem("Open VU", tray_open, default=True),
                pystray.MenuItem("Quit", tray_quit),
            ),
        )
        threading.Thread(target=tray.run, daemon=True).start()
    else:
        # macOS/Linux: drive the tray through Qt's existing main-thread Cocoa
        # event loop. pystray would spin up a second NSApplication run loop on a
        # background thread, which AppKit forbids (-[NSApplication run] is
        # main-thread only) and crashes with a SIGTRAP.
        import io

        from PySide6.QtWidgets import QSystemTrayIcon, QMenu
        from PySide6.QtGui import QAction, QPixmap

        _png = io.BytesIO()
        _make_tray_icon_image().save(_png, format="PNG")
        _tray_pixmap = QPixmap()
        _tray_pixmap.loadFromData(_png.getvalue(), "PNG")
        tray_qicon = QIcon(str(icon_path)) if icon_path.exists() else QIcon(_tray_pixmap)

        tray = QSystemTrayIcon(tray_qicon)
        tray.setToolTip("VU")

        tray_menu = QMenu()
        act_open = QAction("Open VU", tray_menu)
        act_open.triggered.connect(lambda: window.show_with_folder.emit("", None))
        act_quit = QAction("Quit", tray_menu)
        act_quit.triggered.connect(_qt_tray_quit)
        tray_menu.addAction(act_open)
        tray_menu.addAction(act_quit)
        tray.setContextMenu(tray_menu)

        def _on_tray_activated(reason):
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                window.show_with_folder.emit("", None)

        tray.activated.connect(_on_tray_activated)
        tray.show()

    # Holds the macOS Carbon callback + registration refs for the process
    # lifetime — see _install_mac_global_hotkey for why they must not be GC'd.
    _hotkey_refs = None
    # Combo currently bound on Windows; tracked so /api/settings can swap it.
    _current_hotkey = None

    def _install_mac_global_hotkey(callback):
        """Register Ctrl+Shift+V system-wide via the Carbon Event Manager.

        Unlike an NSEvent global monitor this needs NO Accessibility / Input
        Monitoring permission and fires regardless of which app is focused —
        the OS delivers the key event straight to our installed handler, which
        runs on Qt's main (Cocoa) run loop.

        Returns the tuple of C objects the caller must keep alive for the
        process lifetime (the callback + registration refs); if any of them is
        garbage-collected the hotkey goes dead or crashes. Returns None on
        failure.
        """
        try:
            import ctypes
            import ctypes.util

            carbon_path = (
                ctypes.util.find_library("Carbon")
                or "/System/Library/Frameworks/Carbon.framework/Carbon"
            )
            carbon = ctypes.CDLL(carbon_path)

            class EventTypeSpec(ctypes.Structure):
                _fields_ = [("eventClass", ctypes.c_uint32),
                            ("eventKind", ctypes.c_uint32)]

            class EventHotKeyID(ctypes.Structure):
                _fields_ = [("signature", ctypes.c_uint32),
                            ("id", ctypes.c_uint32)]

            # OSStatus handler(EventHandlerCallRef, EventRef, void *userData)
            handler_proto = ctypes.CFUNCTYPE(
                ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
            )

            carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
            carbon.InstallEventHandler.argtypes = [
                ctypes.c_void_p, handler_proto, ctypes.c_uint32,
                ctypes.POINTER(EventTypeSpec), ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            carbon.InstallEventHandler.restype = ctypes.c_int32
            carbon.RegisterEventHotKey.argtypes = [
                ctypes.c_uint32, ctypes.c_uint32, EventHotKeyID,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
            ]
            carbon.RegisterEventHotKey.restype = ctypes.c_int32

            CONTROL_KEY = 0x1000               # controlKey
            SHIFT_KEY = 0x0200                 # shiftKey
            KEYCODE_V = 9                      # kVK_ANSI_V
            EVENT_CLASS_KEYBOARD = 0x6b657962  # 'keyb'
            EVENT_HOTKEY_PRESSED = 6           # kEventHotKeyPressed

            def on_carbon_event(_call_ref, _event, _user_data):
                # Only our single hotkey is registered, so any press is ours.
                # Run off the run loop so the Finder AppleScript can't stall UI.
                threading.Thread(target=callback, daemon=True).start()
                return 0  # noErr

            handler_cb = handler_proto(on_carbon_event)
            handler_ref = ctypes.c_void_p()
            hotkey_ref = ctypes.c_void_p()
            spec = EventTypeSpec(EVENT_CLASS_KEYBOARD, EVENT_HOTKEY_PRESSED)
            hotkey_id = EventHotKeyID(0x56552020, 1)   # signature 'VU  '
            target = carbon.GetApplicationEventTarget()

            carbon.InstallEventHandler(
                target, handler_cb, 1, ctypes.byref(spec), None,
                ctypes.byref(handler_ref),
            )
            carbon.RegisterEventHotKey(
                KEYCODE_V, CONTROL_KEY | SHIFT_KEY, hotkey_id,
                target, 0, ctypes.byref(hotkey_ref),
            )
            return (carbon, handler_cb, handler_ref, hotkey_ref, spec, hotkey_id)
        except Exception as e:
            print(f"VU: macOS hotkey registration failed: {e}", file=sys.stderr)
            return None

    def _register_hotkey(combo: str = "") -> bool:
        """Bind the global hotkey. Empty combo → read from settings. Returns
        True on success. Called at startup and by /api/settings to rebind
        without restarting the app."""
        global _hotkey_refs, _current_hotkey
        if not combo:
            combo = _settings_with_defaults().get("hotkey", DEFAULT_HOTKEY)
        if IS_WIN:
            # Remove the old binding only after the new one registers cleanly,
            # so a bad combo doesn't leave the user with no hotkey at all.
            old = _current_hotkey
            if old:
                try:
                    keyboard.remove_hotkey(old)
                except Exception:
                    pass
            try:
                keyboard.add_hotkey(combo, on_hotkey)
                _current_hotkey = combo
                return True
            except Exception:
                if old:
                    try:
                        keyboard.add_hotkey(old, on_hotkey)
                        _current_hotkey = old
                    except Exception:
                        _current_hotkey = None
                else:
                    _current_hotkey = None
                return False
        if IS_MAC:
            # TODO(mac): the combo is ignored — Carbon registration is hardcoded
            # to Ctrl+Shift+V. Parsing arbitrary combos into Carbon keycodes
            # and modifier flags is the next step for runtime hotkey changes on
            # macOS. Re-registration would also need to UnregisterEventHotKey
            # the existing binding first.
            if _hotkey_refs is not None:
                return True
            _hotkey_refs = _install_mac_global_hotkey(on_hotkey)
            return _hotkey_refs is not None
        return False

    _register_hotkey()
    globals()["_re_register_hotkey"] = _register_hotkey

    sys.exit(qt_app.exec())
