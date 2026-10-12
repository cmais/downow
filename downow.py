#!/usr/bin/env python3
"""downow — baixe o áudio de um vídeo do YouTube em MP3 (ou M4A, sem conversão).

Interface Qt com visual inspirado no GNOME/libadwaita. O motor é o yt-dlp,
carregado de um arquivo .zip (embutido no executável ou atualizado pelo usuário
em ~/.local/share/downow/engine), com ffmpeg e Deno embutidos.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "downow"
APP_VERSION = "1.0.0"
FROZEN = getattr(sys, "frozen", False)
IS_WIN = sys.platform.startswith("win")
USER_AGENT = f"downow/{APP_VERSION}"


# --------------------------------------------------------------------------
# Caminhos e utilitários
# --------------------------------------------------------------------------
def resource_path(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


def data_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stdlib_hints():  # pragma: no cover
    """Nunca é chamada. Faz o PyInstaller incluir módulos da biblioteca padrão
    que o yt-dlp (carregado de um .zip) usa e que o app não importa diretamente."""
    import base64, codecs, collections, concurrent.futures, contextlib, ctypes  # noqa
    import datetime, email.utils, hashlib, hmac, html, html.parser, http.client  # noqa
    import http.cookiejar, http.cookies, itertools, locale, mimetypes, netrc  # noqa
    import optparse, platform, random, secrets, shlex, socket, sqlite3, ssl  # noqa
    import struct, tarfile, time, unicodedata, uuid, xml.etree.ElementTree  # noqa
    import xml.dom.minidom, zlib, bz2, lzma, gzip, string, textwrap, threading  # noqa
    import urllib.error, urllib.request, urllib.response, urllib.parse, json  # noqa
    import calendar, traceback, types, typing, warnings, weakref, getpass, glob  # noqa
    import imageio_ffmpeg, certifi  # noqa


# --------------------------------------------------------------------------
# Motor (yt-dlp carregado de um .zip)
# --------------------------------------------------------------------------
_VERSION_RE = re.compile(r"__version__\s*=\s*['\"]([^'\"]+)['\"]")
_engine = {"module": None, "path": None, "version": None}


def engine_user_zip() -> Path:
    return data_dir() / "engine" / "yt-dlp.zip"


def engine_bundled_zip() -> Path | None:
    p = resource_path("vendor/yt-dlp.zip")
    return p if p.exists() else None


def zip_version(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as z:
            m = _VERSION_RE.search(z.read("yt_dlp/version.py").decode("utf-8"))
            return m.group(1) if m else None
    except Exception:
        return None


def load_engine():
    """Escolhe o .zip mais novo (do usuário ou embutido) e importa o yt-dlp dele."""
    best, best_v = None, ""
    for p in (engine_user_zip(), engine_bundled_zip()):
        if p and p.exists():
            v = zip_version(p)
            if v and v >= best_v:
                best, best_v = p, v

    for name in [m for m in sys.modules
                 if m in ("yt_dlp", "yt_dlp_ejs") or m.startswith(("yt_dlp.", "yt_dlp_ejs."))]:
        del sys.modules[name]
    old = _engine["path"]
    if old and old in sys.path:
        sys.path.remove(old)
    for key in {old, str(best) if best else None}:
        if key:
            sys.path_importer_cache.pop(key, None)
            try:
                import zipimport
                zipimport._zip_directory_cache.pop(key, None)  # type: ignore[attr-defined]
            except Exception:
                pass
    if best:
        sys.path.insert(0, str(best))
        _engine["path"] = str(best)
    importlib.invalidate_caches()
    mod = importlib.import_module("yt_dlp")
    _engine["module"] = mod
    _engine["version"] = importlib.import_module("yt_dlp.version").__version__
    return mod


def engine_module():
    return _engine["module"] or load_engine()


def engine_version() -> str:
    return _engine["version"] or "?"


# --------------------------------------------------------------------------
# ffmpeg e Deno embutidos
# --------------------------------------------------------------------------
def _install_bin(src: str | Path, dst: Path) -> str:
    src = Path(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copy2(src, tmp)
        if not IS_WIN:
            tmp.chmod(0o755)
        os.replace(tmp, dst)
    elif not IS_WIN and not os.access(dst, os.X_OK):
        dst.chmod(0o755)
    return str(dst)


def ensure_binaries() -> tuple[str | None, str | None]:
    bindir = data_dir() / "bin"
    exe = ".exe" if IS_WIN else ""
    ffmpeg = deno = None

    src = None
    try:
        import imageio_ffmpeg
        src = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        src = shutil.which("ffmpeg")
    if src and Path(src).exists():
        ffmpeg = _install_bin(src, bindir / f"ffmpeg{exe}")

    dsrc = resource_path(f"vendor/deno{exe}")
    dsrc = dsrc if dsrc.exists() else (Path(shutil.which("deno")) if shutil.which("deno") else None)
    if dsrc:
        deno = _install_bin(dsrc, bindir / f"deno{exe}")
    return ffmpeg, deno


# --------------------------------------------------------------------------
# Links e mensagens
# --------------------------------------------------------------------------
_YT_HOST = re.compile(r"(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$", re.I)


def normalize_url(text: str) -> tuple[str | None, str | None]:
    """Retorna (url, problema). url é None se o texto não serve."""
    t = text.strip()
    if not t:
        return None, None
    if not re.match(r"^https?://", t, re.I):
        t = "https://" + t
    try:
        u = urllib.parse.urlparse(t)
    except ValueError:
        return None, "Esse link não parece válido."
    if not u.hostname or not _YT_HOST.search(u.hostname):
        return None, "Cole um link do YouTube."
    q = urllib.parse.parse_qs(u.query)
    if u.path.rstrip("/") == "/playlist" and "v" not in q:
        return None, "Esse é o link de uma playlist. Cole o link de um vídeo."
    return t, None


def friendly_error(e: BaseException) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
    low = msg.lower()
    if "private video" in low:
        return "Esse vídeo é privado."
    if "sign in to confirm your age" in low or "age-restricted" in low:
        return "Esse vídeo tem restrição de idade e não pode ser baixado sem login."
    if "not a bot" in low or "sign in to confirm" in low:
        return "O YouTube pediu confirmação de que você não é um robô. Tente mais tarde ou use ⚙ › Atualizar motor."
    if "unavailable" in low or "not available" in low:
        return "Vídeo indisponível. Se ele abre normalmente no navegador, tente ⚙ › Atualizar motor."
    if "urlopen error" in low or "getaddrinfo" in low or "timed out" in low or "network is unreachable" in low:
        return "Sem conexão com a internet."
    if "javascript" in low or "js runtime" in low:
        return "Falha ao resolver o desafio do YouTube. Tente ⚙ › Atualizar motor."
    first = msg.splitlines()[0] if msg else "Erro desconhecido."
    return (first[:220] + "…" if len(first) > 220 else first) + "\nSe o problema continuar, tente ⚙ › Atualizar motor."


def reveal_in_folder(path: str) -> bool:
    p = Path(path)
    try:
        if IS_WIN:
            subprocess.Popen(["explorer", f"/select,{p}"])
            return True
        uri = p.as_uri()
        r = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
             "--object-path", "/org/freedesktop/FileManager1",
             "--method", "org.freedesktop.FileManager1.ShowItems", f"['{uri}']", ""],
            capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# Qt
# --------------------------------------------------------------------------
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

from PySide6.QtCore import (QPointF, QRectF, QSettings, QSize, Qt, QThread, QTimer,  # noqa: E402
                            QUrl, Signal)
from PySide6.QtGui import (QActionGroup, QColor, QDesktopServices, QFont, QGuiApplication,  # noqa: E402
                           QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFileDialog, QFrame, QHBoxLayout,  # noqa: E402
                               QLabel, QLayout, QLineEdit, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QToolButton, QVBoxLayout, QWidget)

LIGHT = dict(bg="#fafafa", card="#ffffff", fg="#2e2e32", dim="#77767b", border="#dcdcdf",
             accent="#3584e4", accent_hover="#478fe8", accent_fg="#ffffff", track="#e4e4e7",
             hover="#ededf0", seg_on="#d6e6fb", success="#26a269", danger="#c01c28")
DARK = dict(bg="#242424", card="#303030", fg="#ffffff", dim="#9a9996", border="#434343",
            accent="#3584e4", accent_hover="#4a90e8", accent_fg="#ffffff", track="#454545",
            hover="#3a3a3a", seg_on="#2b4a73", success="#8ff0a4", danger="#ff7b63")


def build_qss(c: dict) -> str:
    return f"""
    MainWindow {{ background: {c['bg']}; }}
    QLabel {{ background: transparent; color: {c['fg']}; }}
    QLabel#muted {{ color: {c['dim']}; }}
    QLabel#title {{ font-size: 13pt; font-weight: 700; }}
    QLabel#heading {{ font-size: 17pt; font-weight: 700; }}
    QLabel#ok {{ color: {c['success']}; font-size: 14pt; font-weight: 700; }}
    QLabel#err {{ color: {c['danger']}; }}
    QLineEdit#url {{ background: {c['card']}; color: {c['fg']}; border: 1px solid {c['border']};
        border-radius: 10px; padding: 11px 13px; selection-background-color: {c['accent']};
        selection-color: {c['accent_fg']}; }}
    QLineEdit#url:focus {{ border: 2px solid {c['accent']}; padding: 10px 12px; }}
    QToolButton, QPushButton#flat {{ background: {c['card']}; color: {c['fg']};
        border: 1px solid {c['border']}; border-radius: 10px; padding: 10px 14px; }}
    QToolButton:hover, QPushButton#flat:hover {{ background: {c['hover']}; }}
    QToolButton#menu {{ border: none; background: transparent; font-size: 15pt; padding: 4px 10px; }}
    QToolButton#menu:hover {{ background: {c['hover']}; }}
    QToolButton#menu::menu-indicator {{ image: none; }}
    QPushButton#suggested {{ background: {c['accent']}; color: {c['accent_fg']}; border: none;
        border-radius: 10px; padding: 13px 22px; font-weight: 700; font-size: 11.5pt; }}
    QPushButton#suggested:hover {{ background: {c['accent_hover']}; }}
    QPushButton#suggested:disabled {{ background: {c['track']}; color: {c['dim']}; }}
    QPushButton#seg {{ background: {c['card']}; color: {c['fg']}; border: 1px solid {c['border']};
        padding: 7px 14px; }}
    QPushButton#seg:hover {{ background: {c['hover']}; }}
    QPushButton#seg:checked {{ background: {c['seg_on']}; border-color: {c['accent']}; font-weight: 700; }}
    QPushButton#seg:disabled {{ color: {c['dim']}; }}
    QPushButton#segL {{ border-top-left-radius: 9px; border-bottom-left-radius: 9px; }}
    QPushButton#segR {{ border-top-right-radius: 9px; border-bottom-right-radius: 9px; border-left: none; }}
    QFrame#card {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 12px; }}
    QFrame#card QLabel {{ background: transparent; }}
    QProgressBar {{ background: {c['track']}; border: none; border-radius: 4px; max-height: 8px;
        min-height: 8px; }}
    QProgressBar::chunk {{ background: {c['accent']}; border-radius: 4px; }}
    QMenu {{ background: {c['card']}; color: {c['fg']}; border: 1px solid {c['border']};
        border-radius: 8px; padding: 4px; }}
    QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {c['hover']}; color: {c['fg']}; }}
    QMenu::item:disabled {{ color: {c['dim']}; }}
    QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 8px; }}
    QMessageBox {{ background: {c['bg']}; }}
    QMessageBox QPushButton {{ background: {c['card']}; color: {c['fg']}; border: 1px solid {c['border']};
        border-radius: 8px; padding: 7px 18px; min-width: 70px; }}
    QMessageBox QPushButton:hover {{ background: {c['hover']}; }}
    """


def make_palette(c: dict) -> QPalette:
    p = QPalette()
    for role, key in ((QPalette.Window, "bg"), (QPalette.Base, "card"), (QPalette.AlternateBase, "hover"),
                      (QPalette.Button, "card"), (QPalette.ToolTipBase, "card")):
        p.setColor(role, QColor(c[key]))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText, QPalette.ToolTipText):
        p.setColor(role, QColor(c["fg"]))
    p.setColor(QPalette.PlaceholderText, QColor(c["dim"]))
    p.setColor(QPalette.Highlight, QColor(c["accent"]))
    p.setColor(QPalette.HighlightedText, QColor(c["accent_fg"]))
    return p


def check_pixmap(color: str, size: int = 44) -> QPixmap:
    scale = 2
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(2, 2, size * scale - 4, size * scale - 4))
    pen = QPen(QColor("#ffffff" if color != DARK["success"] else "#1e1e1e"), size * scale * 0.09)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    s = size * scale
    p.drawPolyline([QPointF(s * 0.30, s * 0.52), QPointF(s * 0.44, s * 0.66), QPointF(s * 0.71, s * 0.36)])
    p.end()
    pm.setDevicePixelRatio(scale)
    return pm


def rounded_pixmap(src: QPixmap, w: int, h: int, r: int = 8) -> QPixmap:
    scale = 2
    scaled = src.scaled(w * scale, h * scale, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(w * scale, h * scale)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w * scale, h * scale), r * scale, r * scale)
    p.setClipPath(path)
    p.drawPixmap((w * scale - scaled.width()) // 2, (h * scale - scaled.height()) // 2, scaled)
    p.end()
    out.setDevicePixelRatio(scale)
    return out


# --------------------------------------------------------------------------
# Tarefas em segundo plano
# --------------------------------------------------------------------------
class Cancelled(Exception):
    pass


class _QuietLogger:
    def __init__(self):
        self.errors: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.errors.append(str(msg))


class PreviewWorker(QThread):
    ready = Signal(str, str, str, bytes)   # url, título, canal, bytes da miniatura
    failed = Signal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            api = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(self.url, safe="")
            req = urllib.request.Request(api, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.load(r)
            thumb = b""
            if data.get("thumbnail_url"):
                req2 = urllib.request.Request(data["thumbnail_url"], headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req2, timeout=12) as r2:
                    thumb = r2.read()
            self.ready.emit(self.url, data.get("title", ""), data.get("author_name", ""), thumb)
        except Exception:
            self.failed.emit(self.url)


class DownloadWorker(QThread):
    stage = Signal(str)
    progress = Signal(int)          # 0-100, ou -1 = indeterminado
    done = Signal(str)              # caminho do arquivo final
    failed = Signal(str)

    def __init__(self, url: str, dest: str, fmt: str):
        super().__init__()
        self.url, self.dest, self.fmt = url, dest, fmt
        self._cancel = False
        self._final: str | None = None

    def cancel(self):
        self._cancel = True

    def _options(self, ffmpeg, deno, with_thumb: bool, log) -> dict:
        mp3 = self.fmt == "mp3"
        labels = {
            "ExtractAudio": "Convertendo para MP3…" if mp3 else "Preparando o arquivo M4A…",
            "ThumbnailsConvertor": "Preparando a capa…",
            "Metadata": "Gravando informações…",
            "EmbedThumbnail": "Incluindo a capa…",
        }

        def hook(d):
            if self._cancel:
                raise Cancelled()
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    self.progress.emit(min(int(d.get("downloaded_bytes", 0) * 100 / total), 100))
                self.stage.emit("Baixando o áudio…")

        def pp_hook(d):
            if self._cancel:
                raise Cancelled()
            if d.get("status") == "started":
                self.stage.emit(labels.get(d.get("postprocessor"), "Finalizando…"))
                self.progress.emit(-1)
            elif d.get("status") == "finished":
                fp = (d.get("info_dict") or {}).get("filepath")
                if fp:
                    self._final = fp

        pps = []
        if with_thumb:
            pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        if mp3:
            pps.append({"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"})
        else:
            pps.append({"key": "FFmpegExtractAudio", "preferredcodec": "m4a"})
        pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": False})
        if with_thumb:
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

        opts = {
            "format": "bestaudio/best" if mp3 else "bestaudio[ext=m4a]/bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": log,
            "outtmpl": {"default": str(Path(self.dest) / "%(title)s.%(ext)s")},
            "windowsfilenames": IS_WIN,
            "trim_file_name": 180,
            "overwrites": True,
            "writethumbnail": with_thumb,
            "ffmpeg_location": ffmpeg,
            "progress_hooks": [hook],
            "postprocessor_hooks": [pp_hook],
            "postprocessors": pps,
        }
        if deno:
            opts["js_runtimes"] = {"deno": {"path": deno}}
        return opts

    def run(self):
        try:
            self.stage.emit("Preparando…")
            self.progress.emit(-1)
            ffmpeg, deno = ensure_binaries()
            if not ffmpeg:
                raise RuntimeError("ffmpeg não encontrado.")
            yt_dlp = engine_module()
            Path(self.dest).mkdir(parents=True, exist_ok=True)

            info = None
            for attempt, with_thumb in enumerate((True, False)):
                log = _QuietLogger()
                self._final = None
                self.stage.emit("Conectando ao YouTube…")
                self.progress.emit(-1)
                try:
                    with yt_dlp.YoutubeDL(self._options(ffmpeg, deno, with_thumb, log)) as ydl:
                        info = ydl.extract_info(self.url, download=True)
                    break
                except Cancelled:
                    raise
                except Exception as e:  # tenta de novo sem capa se o problema foi a capa
                    text = str(e).lower() + " ".join(log.errors).lower()
                    if with_thumb and ("thumbnail" in text or "postprocessing" in text):
                        continue
                    if self._cancel:
                        raise Cancelled()
                    raise

            final = self._final
            if not final and info:
                rd = info.get("requested_downloads") or []
                final = rd[0].get("filepath") if rd else None
            if not final or not Path(final).exists():
                raise RuntimeError("O arquivo final não foi encontrado após o download.")
            self.progress.emit(100)
            self.done.emit(final)
        except Cancelled:
            self.failed.emit("")
        except Exception as e:
            self.failed.emit("" if self._cancel else friendly_error(e))


class UpdateWorker(QThread):
    result = Signal(bool, str)   # (atualizou?, mensagem)

    def run(self):
        try:
            current = engine_version()
            req = urllib.request.Request(
                "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
                headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                tag = json.load(r)["tag_name"]
            if tag <= current:
                self.result.emit(False, f"O motor já está na versão mais recente ({current}).")
                return
            url = f"https://github.com/yt-dlp/yt-dlp/releases/download/{tag}/yt-dlp"
            target = engine_user_zip()
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".part")
            os.close(fd)
            try:
                req2 = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req2, timeout=60) as r2, open(tmp, "wb") as f:
                    shutil.copyfileobj(r2, f)
                if zip_version(Path(tmp)) is None:
                    raise RuntimeError("O arquivo baixado não é um motor válido.")
                os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            self.result.emit(True, tag)
        except Exception as e:
            self.result.emit(False, "Não foi possível atualizar: " + friendly_error(e).splitlines()[0])


# --------------------------------------------------------------------------
# Janela principal
# --------------------------------------------------------------------------
class MainWindow(QWidget):
    def __init__(self, app: QApplication, system_dark: bool):
        super().__init__()
        self.app = app
        self.system_dark = system_dark
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.url: str | None = None
        self.final_path: str | None = None
        self.preview_worker: PreviewWorker | None = None
        self.download_worker: DownloadWorker | None = None
        self.update_worker: UpdateWorker | None = None
        self.busy = False

        self.setWindowTitle(APP_NAME)
        self.setAttribute(Qt.WA_StyledBackground, True)
        icon_path = resource_path("assets/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setFixedWidth(540)

        self.dest = self.settings.value("dest", "", str) or self._default_dest()
        self.fmt = self.settings.value("fmt", "mp3", str)
        self.theme_pref = self.settings.value("theme", "auto", str)

        self._build_ui()
        self.apply_theme()
        self.app.styleHints().colorSchemeChanged.connect(lambda *_: self.apply_theme())

        self.debounce = QTimer(self, singleShot=True, interval=450)
        self.debounce.timeout.connect(self._start_preview)

    # ---- construção ------------------------------------------------------
    def _default_dest(self) -> str:
        from PySide6.QtCore import QStandardPaths
        music = QStandardPaths.writableLocation(QStandardPaths.MusicLocation)
        return music or str(Path.home())

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 14, 26, 26)
        root.setSpacing(16)
        root.setSizeConstraint(QLayout.SetFixedSize)

        # cabeçalho
        head = QHBoxLayout()
        logo = QLabel()
        icon_path = resource_path("assets/icon.png")
        if icon_path.exists():
            logo.setPixmap(QPixmap(str(icon_path)).scaled(30, 30, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        name = QLabel(APP_NAME)
        name.setObjectName("title")
        head.addWidget(logo)
        head.addWidget(name)
        head.addStretch(1)
        self.menu_btn = QToolButton()
        self.menu_btn.setObjectName("menu")
        self.menu_btn.setText("⚙")
        self.menu_btn.setCursor(Qt.PointingHandCursor)
        self.menu_btn.setPopupMode(QToolButton.InstantPopup)
        self.menu_btn.setMenu(self._build_menu())
        head.addWidget(self.menu_btn)
        root.addLayout(head)

        heading = QLabel("Cole o link do vídeo")
        heading.setObjectName("heading")
        root.addWidget(heading)

        # campo de URL + colar
        row = QHBoxLayout()
        row.setSpacing(8)
        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("url")
        self.url_edit.setPlaceholderText("https://www.youtube.com/watch?v=…")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.textChanged.connect(self._on_text_changed)
        self.url_edit.returnPressed.connect(self._on_download_clicked)
        self.paste_btn = QToolButton()
        self.paste_btn.setText("Colar")
        self.paste_btn.setCursor(Qt.PointingHandCursor)
        self.paste_btn.clicked.connect(self._paste)
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.paste_btn)
        root.addLayout(row)

        self.hint = QLabel("")
        self.hint.setObjectName("err")
        self.hint.setWordWrap(True)
        self.hint.hide()
        root.addWidget(self.hint)

        # cartão de prévia
        self.preview = QFrame()
        self.preview.setObjectName("card")
        pl = QHBoxLayout(self.preview)
        pl.setContentsMargins(12, 12, 14, 12)
        pl.setSpacing(12)
        self.thumb = QLabel()
        self.thumb.setFixedSize(128, 72)
        self.p_title = QLabel()
        self.p_title.setWordWrap(True)
        self.p_title.setStyleSheet("font-weight: 700;")
        self.p_author = QLabel()
        self.p_author.setObjectName("muted")
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addStretch(1)
        col.addWidget(self.p_title)
        col.addWidget(self.p_author)
        col.addStretch(1)
        pl.addWidget(self.thumb)
        pl.addLayout(col, 1)
        self.preview.hide()
        root.addWidget(self.preview)

        # formato (seletor discreto)
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.btn_mp3 = QPushButton("MP3 · 320 kbps")
        self.btn_m4a = QPushButton("M4A · sem conversão")
        for b, extra in ((self.btn_mp3, "segL"), (self.btn_m4a, "segR")):
            b.setObjectName("seg")
            b.setProperty("class", extra)
            b.setCheckable(True)
            b.setMinimumWidth(158)
            b.setCursor(Qt.PointingHandCursor)
        self.btn_mp3.setObjectName("seg")
        self._fmt_group = QButtonGroup(self)
        self._fmt_group.setExclusive(True)
        self._fmt_group.addButton(self.btn_mp3)
        self._fmt_group.addButton(self.btn_m4a)
        (self.btn_mp3 if self.fmt == "mp3" else self.btn_m4a).setChecked(True)
        self.btn_mp3.toggled.connect(self._on_format_changed)
        seg.addStretch(1)
        seg.addWidget(self.btn_mp3)
        seg.addWidget(self.btn_m4a)
        seg.addStretch(1)
        root.addLayout(seg)

        # botão principal
        self.main_btn = QPushButton()
        self.main_btn.setObjectName("suggested")
        self.main_btn.setCursor(Qt.PointingHandCursor)
        self.main_btn.clicked.connect(self._on_download_clicked)
        root.addWidget(self.main_btn)
        self._refresh_main_button()

        # progresso
        self.progress_box = QWidget()
        pbl = QVBoxLayout(self.progress_box)
        pbl.setContentsMargins(0, 0, 0, 0)
        pbl.setSpacing(8)
        self.stage_lbl = QLabel("")
        self.stage_lbl.setObjectName("muted")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        pbl.addWidget(self.stage_lbl)
        pbl.addWidget(self.bar)
        self.progress_box.hide()
        root.addWidget(self.progress_box)

        # concluído
        self.done_box = QFrame()
        self.done_box.setObjectName("card")
        dl = QVBoxLayout(self.done_box)
        dl.setContentsMargins(16, 16, 16, 16)
        dl.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.check_lbl = QLabel()
        self.check_lbl.setFixedSize(44, 44)
        txt = QVBoxLayout()
        txt.setSpacing(2)
        ok = QLabel("Download completo")
        ok.setObjectName("ok")
        self.file_lbl = QLabel("")
        self.file_lbl.setObjectName("muted")
        self.file_lbl.setWordWrap(True)
        txt.addWidget(ok)
        txt.addWidget(self.file_lbl)
        top.addWidget(self.check_lbl)
        top.addLayout(txt, 1)
        dl.addLayout(top)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.open_btn = QPushButton("Abrir pasta")
        self.open_btn.setObjectName("suggested")
        self.open_btn.setCursor(Qt.PointingHandCursor)
        self.open_btn.clicked.connect(self._open_folder)
        self.again_btn = QPushButton("Baixar outro")
        self.again_btn.setObjectName("flat")
        self.again_btn.setCursor(Qt.PointingHandCursor)
        self.again_btn.clicked.connect(self._reset_for_next)
        btns.addWidget(self.open_btn, 1)
        btns.addWidget(self.again_btn)
        dl.addLayout(btns)
        self.done_box.hide()
        root.addWidget(self.done_box)

        self.error_lbl = QLabel("")
        self.error_lbl.setObjectName("err")
        self.error_lbl.setWordWrap(True)
        self.error_lbl.hide()
        root.addWidget(self.error_lbl)

    def _build_menu(self) -> QMenu:
        m = QMenu(self)
        self.dest_info = m.addAction("")
        self.dest_info.setEnabled(False)
        m.addAction("Escolher pasta de destino…", self._choose_dest)
        m.addSeparator()
        theme = m.addMenu("Aparência")
        grp = QActionGroup(self)
        for key, label in (("auto", "Automática"), ("light", "Clara"), ("dark", "Escura")):
            a = theme.addAction(label)
            a.setCheckable(True)
            a.setChecked(self.theme_pref == key)
            a.triggered.connect(lambda _=False, k=key: self._set_theme(k))
            grp.addAction(a)
        m.addSeparator()
        self.update_action = m.addAction("", self._update_engine)
        m.addAction("Sobre o downow", self._about)
        self._refresh_menu_text()
        return m

    def _refresh_menu_text(self):
        d = self.dest if len(self.dest) < 42 else "…" + self.dest[-41:]
        self.dest_info.setText(f"Pasta: {d}")
        self.update_action.setText(f"Atualizar motor (yt-dlp {engine_version()})")

    # ---- tema ------------------------------------------------------------
    def _is_dark(self) -> bool:
        pref = os.environ.get("DOWNOW_THEME", self.theme_pref)
        if pref == "dark":
            return True
        if pref == "light":
            return False
        scheme = self.app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
        return self.system_dark

    def apply_theme(self):
        c = DARK if self._is_dark() else LIGHT
        self.app.setPalette(make_palette(c))
        self.app.setStyleSheet(build_qss(c))
        self.check_lbl.setPixmap(check_pixmap(c["success"]))
        # propriedade "class" dos segmentos (cantos arredondados só nas pontas)
        self.btn_mp3.setStyleSheet(
            "QPushButton{border-top-left-radius:9px;border-bottom-left-radius:9px;"
            "border-top-right-radius:0;border-bottom-right-radius:0;}")
        self.btn_m4a.setStyleSheet(
            "QPushButton{border-top-right-radius:9px;border-bottom-right-radius:9px;"
            "border-top-left-radius:0;border-bottom-left-radius:0;border-left:none;}"
            "QPushButton:checked{border-left:1px solid %s;}" % c["accent"])

    def _set_theme(self, key: str):
        self.theme_pref = key
        self.settings.setValue("theme", key)
        self.apply_theme()

    # ---- ações da interface ---------------------------------------------
    def _paste(self):
        text = QGuiApplication.clipboard().text().strip()
        if text:
            self.url_edit.setText(text)
        self.url_edit.setFocus()

    def _on_format_changed(self):
        self.fmt = "mp3" if self.btn_mp3.isChecked() else "m4a"
        self.settings.setValue("fmt", self.fmt)
        self._refresh_main_button()

    def _refresh_main_button(self):
        if self.busy and self.download_worker:
            self.main_btn.setText("Cancelar")
        else:
            self.main_btn.setText("Baixar MP3" if self.fmt == "mp3" else "Baixar M4A")

    def _choose_dest(self):
        d = QFileDialog.getExistingDirectory(self, "Pasta de destino", self.dest)
        if d:
            self.dest = d
            self.settings.setValue("dest", d)
            self._refresh_menu_text()

    def _on_text_changed(self, text: str):
        if self.busy:
            return
        self.done_box.hide()
        self.error_lbl.hide()
        self.hint.hide()
        self.preview.hide()
        self.url = None
        url, problem = normalize_url(text)
        if problem:
            self.hint.setText(problem)
            self.hint.show()
        elif url:
            self.url = url
            self.debounce.start()
        self._set_buttons_enabled()

    def _set_buttons_enabled(self):
        idle = not self.busy
        self.url_edit.setEnabled(idle)
        self.paste_btn.setEnabled(idle)
        self.btn_mp3.setEnabled(idle)
        self.btn_m4a.setEnabled(idle)
        self.menu_btn.setEnabled(idle)
        if self.busy:
            self.main_btn.setEnabled(self.download_worker is not None)
        else:
            self.main_btn.setEnabled(bool(self.url))

    # ---- prévia ------------------------------------------------------------
    def _start_preview(self):
        if not self.url or self.busy:
            return
        w = PreviewWorker(self.url)
        w.ready.connect(self._on_preview)
        w.failed.connect(self._on_preview_failed)
        w.finished.connect(w.deleteLater)
        self.preview_worker = w
        w.start()

    def _on_preview(self, url: str, title: str, author: str, thumb: bytes):
        if url != self.url or self.busy:
            return
        self.p_title.setText(title or url)
        self.p_author.setText(author)
        pm = QPixmap()
        if thumb and pm.loadFromData(thumb):
            self.thumb.setPixmap(rounded_pixmap(pm, 128, 72, 8))
            self.thumb.show()
        else:
            self.thumb.hide()
        self.preview.show()

    def _on_preview_failed(self, url: str):
        if url != self.url or self.busy:
            return
        self.hint.setText("Não consegui mostrar a prévia (vídeo privado ou sem internet?). "
                          "Você ainda pode tentar baixar.")
        self.hint.show()

    # ---- download ------------------------------------------------------------
    def _on_download_clicked(self):
        if self.busy and self.download_worker:
            self.main_btn.setEnabled(False)
            self.stage_lbl.setText("Cancelando…")
            self.download_worker.cancel()
            return
        if self.busy or not self.url:
            return
        self.hint.hide()
        self.error_lbl.hide()
        self.done_box.hide()
        self.busy = True
        w = DownloadWorker(self.url, self.dest, self.fmt)
        w.stage.connect(self.stage_lbl.setText)
        w.progress.connect(self._on_progress)
        w.done.connect(self._on_done)
        w.failed.connect(self._on_failed)
        w.finished.connect(w.deleteLater)
        self.download_worker = w
        self.stage_lbl.setText("Preparando…")
        self.bar.setRange(0, 0)
        self.progress_box.show()
        self._refresh_main_button()
        self._set_buttons_enabled()
        w.start()

    def _on_progress(self, pct: int):
        if pct < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(pct)

    def _finish_busy(self):
        self.busy = False
        self.download_worker = None
        self.progress_box.hide()
        self._refresh_main_button()
        self._set_buttons_enabled()

    def _on_done(self, path: str):
        self._finish_busy()
        self.final_path = path
        self.file_lbl.setText(Path(path).name)
        self.done_box.show()

    def _on_failed(self, message: str):
        self._finish_busy()
        if message:
            self.error_lbl.setText(message)
            self.error_lbl.show()

    def _open_folder(self):
        if not self.final_path:
            return
        if not reveal_in_folder(self.final_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.final_path).parent)))

    def _reset_for_next(self):
        self.done_box.hide()
        self.preview.hide()
        self.url_edit.clear()
        self.url_edit.setFocus()

    # ---- motor / sobre ---------------------------------------------------------
    def _update_engine(self):
        if self.busy:
            return
        self.busy = True
        self._set_buttons_enabled()
        self.stage_lbl.setText("Procurando atualização do motor…")
        self.bar.setRange(0, 0)
        self.progress_box.show()
        w = UpdateWorker()
        w.result.connect(self._on_update_result)
        w.finished.connect(w.deleteLater)
        self.update_worker = w
        w.start()

    def _on_update_result(self, updated: bool, message: str):
        self.busy = False
        self.progress_box.hide()
        self._set_buttons_enabled()
        if updated:
            try:
                load_engine()
                self._refresh_menu_text()
                QMessageBox.information(self, APP_NAME, f"Motor atualizado para o yt-dlp {engine_version()}.")
            except Exception as e:
                QMessageBox.warning(self, APP_NAME, f"O motor foi baixado, mas não pôde ser carregado: {e}\n"
                                                    "Feche e abra o downow novamente.")
        else:
            QMessageBox.information(self, APP_NAME, message)

    def _about(self):
        QMessageBox.about(
            self, "Sobre o downow",
            f"<b>downow {APP_VERSION}</b><br>Baixa o áudio de um vídeo do YouTube em MP3 ou M4A.<br><br>"
            f"Motor: yt-dlp {engine_version()}<br>Conversão: ffmpeg<br><br>"
            "Use apenas com vídeos seus, livres ou com permissão de quem os publicou.")

    def closeEvent(self, e):
        for w in (self.download_worker, self.preview_worker, self.update_worker):
            try:
                if w and w.isRunning():
                    if isinstance(w, DownloadWorker):
                        w.cancel()
                    w.wait(4000)
            except RuntimeError:
                pass
        super().closeEvent(e)


# --------------------------------------------------------------------------
# Autoteste (usado pelo GitHub Actions): python downow.py --selftest
# --------------------------------------------------------------------------
def selftest() -> int:
    ok = True

    def log(msg):
        try:
            print(msg, flush=True)
        except Exception:
            pass

    try:
        load_engine()
        log(f"yt-dlp {engine_version()} carregado de {_engine['path'] or 'site-packages'}")
        ejs = importlib.import_module("yt_dlp_ejs")
        log(f"yt-dlp-ejs: {getattr(ejs, '__file__', 'ok')}")
    except Exception as e:
        log(f"FALHA no motor: {e!r}")
        return 1

    ffmpeg, deno = ensure_binaries()
    env = os.environ.copy()
    if ffmpeg:
        out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
        log(out)
        with tempfile.TemporaryDirectory() as td:
            mp3 = Path(td) / "t.mp3"
            r = subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                                "-b:a", "320k", str(mp3)], capture_output=True, text=True, env=env)
            if r.returncode != 0 or not mp3.exists() or mp3.stat().st_size < 1000:
                log("FALHA: ffmpeg não conseguiu gerar MP3 (libmp3lame?)")
                ok = False
            else:
                log(f"MP3 de teste gerado ({mp3.stat().st_size} bytes)")
    else:
        log("FALHA: ffmpeg não encontrado")
        ok = False

    if deno:
        r = subprocess.run([deno, "--version"], capture_output=True, text=True, env=env)
        log(r.stdout.splitlines()[0] if r.returncode == 0 else f"FALHA no deno: {r.stderr}")
        ok = ok and r.returncode == 0
    else:
        log("FALHA: Deno não encontrado")
        ok = False

    try:
        with engine_module().YoutubeDL({"quiet": True, "js_runtimes": {"deno": {"path": deno}} if deno else {}}):
            log("YoutubeDL inicializado com js_runtimes")
    except Exception as e:
        log(f"FALHA ao iniciar YoutubeDL: {e!r}")
        ok = False
    log("AUTOTESTE OK" if ok else "AUTOTESTE FALHOU")
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_NAME)
    system_dark = app.palette().window().color().lightness() < 128
    app.setStyle("Fusion")

    # O PyInstaller aponta LD_LIBRARY_PATH para as bibliotecas embutidas; programas
    # externos (gdbus, ffmpeg, deno) devem usar as do sistema.
    if FROZEN and not IS_WIN:
        orig = os.environ.get("LD_LIBRARY_PATH_ORIG")
        if orig is not None:
            os.environ["LD_LIBRARY_PATH"] = orig
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)

    try:
        load_engine()
    except Exception as e:
        QMessageBox.critical(None, APP_NAME, f"Não foi possível carregar o motor (yt-dlp):\n{e}")
        return 1

    win = MainWindow(app, system_dark)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
