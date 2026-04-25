"""
Fetch a static ffmpeg binary into assets/ffmpeg/<platform>/ so the build
can bundle it. Idempotent — skips if the file already exists.
"""
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET_BASE = ROOT / "assets" / "ffmpeg"


def _download(url: str) -> bytes:
    print(f"[..] downloading {url}")
    with urllib.request.urlopen(url) as r:
        return r.read()


def windows():
    out = TARGET_BASE / "win" / "ffmpeg.exe"
    if out.exists():
        print(f"[ok] {out} already exists")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    data = _download("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip")
    print("[..] extracting ffmpeg.exe")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        member = next((n for n in z.namelist() if n.endswith("/bin/ffmpeg.exe")), None)
        if not member:
            raise RuntimeError("ffmpeg.exe not found in archive")
        with z.open(member) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
    print(f"[ok] wrote {out} ({out.stat().st_size // 1024 // 1024} MB)")


def mac():
    out = TARGET_BASE / "mac" / "ffmpeg"
    if out.exists():
        print(f"[ok] {out} already exists")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    data = _download("https://evermeet.cx/ffmpeg/getrelease/zip")
    print("[..] extracting ffmpeg")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        member = next((n for n in z.namelist() if n.rstrip("/").endswith("ffmpeg")), None)
        if not member:
            raise RuntimeError("ffmpeg binary not found in archive")
        with z.open(member) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
    out.chmod(0o755)
    print(f"[ok] wrote {out} ({out.stat().st_size // 1024 // 1024} MB)")


if sys.platform == "win32":
    windows()
elif sys.platform == "darwin":
    mac()
else:
    print("Linux: install ffmpeg via your package manager (apt, dnf, etc.)")
    sys.exit(0)
