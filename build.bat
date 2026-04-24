@echo off
setlocal

echo [VU] installing build dependencies...
python -m pip install -q pyinstaller
python -m pip install -q -r requirements.txt

echo [VU] building...
python -m PyInstaller ^
  --noconfirm ^
  --onefile ^
  --noconsole ^
  --name VU ^
  --icon "assets\vu.ico" ^
  --add-data "static;static" ^
  --collect-submodules webview ^
  --collect-submodules pystray ^
  --hidden-import pystray._win32 ^
  --hidden-import keyboard ^
  --hidden-import win32com.client ^
  --hidden-import win32gui ^
  --hidden-import pythoncom ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols ^
  --hidden-import uvicorn.protocols.http ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.websockets ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.lifespan ^
  --hidden-import uvicorn.lifespan.on ^
  main.py

echo.
echo [VU] done. run dist\VU.exe
