# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir：桌面窗口 + 本地 FastAPI 服务打进同一目录。
# 构建：python -m PyInstaller --noconfirm DocStudio.spec  → dist/DocStudio/
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [("static", "static"), ("glossary.json", ".")]
# pymupdf 导入时会激活 layout 模块并读取 resources 下的 yaml，缺文件会直接抛 FileNotFoundError。
datas += collect_data_files("pymupdf")
binaries = []
hiddenimports = ["webview.platforms.edgechromium", "clr"]

for package in ("uvicorn", "fastapi", "starlette", "webview", "pythonnet", "clr_loader"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# torch / simple_lama_inpainting 是可选的照片擦除链路，体积以 GB 计，默认不打进安装包；
# 需要时按 README 的可选依赖说明在本机源码环境使用。
a = Analysis(
    ["desktop_app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["torch", "torchvision", "torchaudio", "simple_lama_inpainting", "matplotlib",
              "tkinter", "PySide6", "PyQt5", "PyQt6", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DocStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="DocStudio")
