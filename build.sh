#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Detect Python (macOS usually has python3)
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: python3 not found on PATH"
  exit 1
fi

echo "[VU] checking ffmpeg..."
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "  ffmpeg not found. Install with:  brew install ffmpeg"
  echo "  (Build will continue; video thumbs won't generate without it.)"
fi

echo "[VU] installing build dependencies..."
"$PY" -m pip install -q pyinstaller
"$PY" -m pip install -q -r requirements.txt

echo "[VU] generating icons..."
"$PY" gen_icon.py || echo "  (icon generation failed — continuing without)"

ICON_ARG=()
if [ -f "assets/vu.icns" ]; then
  ICON_ARG=(--icon "assets/vu.icns")
fi

echo "[VU] building..."
"$PY" -m PyInstaller \
  --noconfirm \
  --onefile \
  --windowed \
  --name VU \
  "${ICON_ARG[@]}" \
  --add-data "static:static" \
  --collect-submodules webview \
  --collect-submodules pystray \
  --hidden-import keyboard \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops \
  --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.protocols \
  --hidden-import uvicorn.protocols.http \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan \
  --hidden-import uvicorn.lifespan.on \
  main.py

echo ""
echo "[VU] done. Output:"
echo "  dist/VU.app   (preferred — double-click to launch)"
echo "  dist/VU       (single-file binary, if only that was produced)"
echo ""
echo "Note: On first launch, macOS will ask you to grant Accessibility permission"
echo "      so the global Ctrl+Shift+V hotkey can register. System Settings →"
echo "      Privacy & Security → Accessibility → add VU."
