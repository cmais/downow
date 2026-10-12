#!/usr/bin/env python3
"""Gera o executável único do downow para o sistema atual (Linux ou Windows).

    pip install -r requirements.txt pyinstaller
    python build.py

Baixa o motor (yt-dlp, em .zip) e o Deno para ./vendor, depois roda o PyInstaller.
O resultado fica em dist/downow (Linux) ou dist/downow.exe (Windows).
"""
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"
IS_WIN = sys.platform.startswith("win")
UA = {"User-Agent": "downow-build"}


def download(url: str, dest: Path) -> None:
    print(f"baixando {url}")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def fetch_vendor() -> None:
    VENDOR.mkdir(exist_ok=True)
    download("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp", VENDOR / "yt-dlp.zip")
    if not zipfile.is_zipfile(VENDOR / "yt-dlp.zip"):
        raise SystemExit("yt-dlp baixado não é um zip válido")

    deno_name = "deno.exe" if IS_WIN else "deno"
    if not (VENDOR / deno_name).exists():
        asset = "deno-x86_64-pc-windows-msvc.zip" if IS_WIN else "deno-x86_64-unknown-linux-gnu.zip"
        tmp = VENDOR / "deno.zip"
        download(f"https://github.com/denoland/deno/releases/latest/download/{asset}", tmp)
        with zipfile.ZipFile(tmp) as z:
            z.extract(deno_name, VENDOR)
        tmp.unlink()
        if not IS_WIN:
            (VENDOR / deno_name).chmod(0o755)


def stdlib_imports_from_zip(zip_path: Path) -> list[str]:
    """Lista os módulos da biblioteca padrão que o yt-dlp importa. Como o yt-dlp
    vai num .zip (fora da análise do PyInstaller), eles precisam ser pedidos."""
    import ast

    found: set[str] = set()
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".py"):
                continue
            try:
                tree = ast.parse(z.read(name))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module]
                    mods += [f"{node.module}.{a.name}" for a in node.names if a.name != "*"]
                for m in mods:
                    if m.split(".")[0] in sys.stdlib_module_names:
                        found.add(m)
    skip = {"tkinter", "idlelib", "turtle", "test", "lib2to3"}
    return sorted(m for m in found if m.split(".")[0] not in skip)


def build() -> None:
    import PyInstaller.__main__ as pyi

    sep = os.pathsep
    args = [
        str(ROOT / "downow.py"),
        "--name", "downow",
        "--onefile",
        "--noconfirm",
        "--clean",
        "--windowed" if IS_WIN else "--console",
        "--icon", str(ROOT / "assets" / ("icon.ico" if IS_WIN else "icon.png")),
        "--add-data", f"{ROOT / 'assets' / 'icon.png'}{sep}assets",
        "--add-data", f"{VENDOR / 'yt-dlp.zip'}{sep}vendor",
        "--add-data", f"{VENDOR / ('deno.exe' if IS_WIN else 'deno')}{sep}vendor",
        "--collect-all", "imageio_ffmpeg",
        "--collect-all", "certifi",
        "--exclude-module", "yt_dlp",
        "--exclude-module", "yt_dlp_ejs",
        "--exclude-module", "tkinter",
    ]
    for mod in stdlib_imports_from_zip(VENDOR / "yt-dlp.zip"):
        args += ["--hidden-import", mod]
    pyi.run(args)


if __name__ == "__main__":
    fetch_vendor()
    build()
    print("\nPronto:", ROOT / "dist" / ("downow.exe" if IS_WIN else "downow"))
