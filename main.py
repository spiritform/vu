import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# PyInstaller --noconsole: stdout/stderr are None, which breaks any print/log call
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from fastapi import FastAPI, HTTPException, Query
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
state = {"root": None, "hidden": set()}


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
    # fresh scan resets the session hidden list
    state["hidden"] = set()
    hidden = state["hidden"]

    items = []
    for p in iterator:
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in MEDIA_EXTS:
            continue
        rel = p.relative_to(root).as_posix()
        if rel in hidden:
            continue
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
    return {"root": str(root), "count": len(items), "items": items, "hidden_count": len(hidden)}


class DropBody(BaseModel):
    paths: list[str]


@app.post("/api/drop")
def drop(body: DropBody):
    if not body.paths:
        raise HTTPException(400, "no paths")
    first = Path(body.paths[0])
    if first.is_dir():
        return {"folder": str(first.resolve())}
    if first.exists():
        return {"folder": str(first.parent.resolve())}
    raise HTTPException(400, "path not found")


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


@app.post("/api/hide")
def hide(body: PathBody):
    root = state["root"]
    if not root:
        raise HTTPException(400, "No folder scanned")
    validate_path(body.path)
    state["hidden"].add(body.path)
    # also unheart since it's no longer shown
    hearts = load_hearts(root)
    if body.path in hearts:
        hearts.discard(body.path)
        save_hearts(root, hearts)
    return {"ok": True, "hidden_count": len(state["hidden"])}


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


def _get_explorer_folder():
    """Return the folder path of the foreground file-manager window, or None."""
    if IS_WIN:
        try:
            import pythoncom
            import win32gui
            import win32com.client

            pythoncom.CoInitialize()
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            shell = win32com.client.Dispatch("Shell.Application")
            for w in shell.Windows():
                try:
                    if int(w.HWND) == hwnd:
                        return w.Document.Folder.Self.Path
                except Exception:
                    continue
        except Exception:
            return None
        return None

    if IS_MAC:
        try:
            script = (
                'try\n'
                '    return POSIX path of (target of front Finder window as alias)\n'
                'on error\n'
                '    return ""\n'
                'end try'
            )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=2,
            )
            path = result.stdout.strip()
            return path or None
        except Exception:
            return None

    return None


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
    import uvicorn
    import pystray
    import keyboard

    from PySide6.QtWidgets import QApplication, QMainWindow, QFileDialog
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtCore import QUrl, Qt, QEvent, Signal, QTimer
    from PySide6.QtGui import QIcon

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

        def _accept_if_files(self, e) -> bool:
            md = e.mimeData()
            if md and md.hasUrls() and any(u.isLocalFile() for u in md.urls()):
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
                if e.mimeData().hasUrls():
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
            if e.mimeData().hasUrls():
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
        # cross-thread "open me with this folder" signal — empty string means just show
        show_with_folder = Signal(str)

        def __init__(self):
            super().__init__()
            self.setWindowTitle("VU")
            if icon_path.exists():
                self.setWindowIcon(QIcon(str(icon_path)))
            self.resize(1400, 900)
            self._last_save_dir = None

            self.view = WebView(self)
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

        def _handle_show(self, folder: str):
            self.show()
            self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
            self.activateWindow()
            self.raise_()
            if folder:
                self.view.page().runJavaScript(
                    f"window.openFolder({json.dumps(folder)})"
                )

        def _on_dropped(self, paths: list):
            # Make sure window is visible when something gets dropped
            if not self.isVisible():
                self._handle_show("")
            self.view.page().runJavaScript(
                f"window.openDropped({json.dumps(paths)})"
            )

    qt_app = QApplication.instance() or QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)
    if icon_path.exists():
        qt_app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()  # starts hidden — user invokes via hotkey or tray

    def on_hotkey():
        folder = _get_explorer_folder() or ""
        window.show_with_folder.emit(folder)

    def tray_open(icon, item):
        window.show_with_folder.emit("")

    def tray_quit(icon, item):
        try:
            tray.stop()
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

    tray = pystray.Icon(
        "VU",
        _make_tray_icon_image(),
        "VU — Ctrl+Shift+V",
        menu=pystray.Menu(
            pystray.MenuItem("Open VU", tray_open, default=True),
            pystray.MenuItem("Quit", tray_quit),
        ),
    )
    threading.Thread(target=tray.run, daemon=True).start()

    try:
        keyboard.add_hotkey("ctrl+shift+v", on_hotkey)
    except Exception:
        pass

    sys.exit(qt_app.exec())
