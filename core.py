"""Download de playlists do YouTube e transcrição local."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from urllib.request import urlretrieve

import autorizacoes
import transcricao

ROOT = Path(__file__).resolve().parent
DOWNLOADS = ROOT / "downloads"
MODELS = ROOT / "models"
INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "music.youtube.com",
}

ProgressFn = Callable[[dict[str, Any]], None]


class UrlError(ValueError):
    pass


def safe_name(value: str, max_len: int = 80) -> str:
    cleaned = INVALID_FS.sub(" ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:max_len] or "video").rstrip(" .")


def parse_youtube_url(url: str) -> dict[str, str | None]:
    raw = (url or "").strip()
    if not raw:
        raise UrlError("Cole um link do YouTube.")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise UrlError("Só aceito links do YouTube.")

    query = parse_qs(parsed.query)
    playlist_id = (query.get("list") or [None])[0]
    video_id = (query.get("v") or [None])[0]
    path = parsed.path.rstrip("/")

    if host.endswith("youtu.be"):
        video_id = video_id or path.lstrip("/")
    elif path.startswith("/shorts/"):
        video_id = video_id or path.split("/")[-1]
    elif path.startswith("/embed/"):
        video_id = video_id or path.split("/")[-1]
    elif path.startswith("/live/"):
        video_id = video_id or path.split("/")[-1]
    elif path.startswith("/playlist") and playlist_id:
        video_id = None

    if playlist_id:
        return {"kind": "playlist", "playlist_id": playlist_id, "video_id": video_id}
    if video_id:
        return {"kind": "video", "playlist_id": None, "video_id": video_id}
    raise UrlError("Não achei o vídeo nem a playlist nesse link.")


def vtt_to_text(vtt: str) -> str:
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line.startswith("WEBVTT") or line.startswith("NOTE") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if "-->" in line or re.fullmatch(r"\d+", line):
            continue
        line = re.sub(r"<[^>]+>", "", line).replace("&nbsp;", " ").strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines).strip()


_FFMPEG_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
    Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "ffmpeg" / "bin" / "ffmpeg.exe",
)


def ffmpeg_path() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in _FFMPEG_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget.is_dir():
        matches = sorted(winget.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"))
        if matches:
            return str(matches[-1])
    return None


def which_tools() -> dict[str, Any]:
    whisper = False
    try:
        import faster_whisper  # noqa: F401

        whisper = True
    except Exception:
        whisper = False
    vosk = False
    try:
        import vosk  # noqa: F401

        vosk = True
    except Exception:
        vosk = False
    return {
        "ffmpeg": bool(ffmpeg_path()),
        "whisper": whisper,
        "vosk": vosk,
        "downloads": str(DOWNLOADS),
    }


def format_selector(quality: str, has_ffmpeg: bool) -> str:
    if quality == "audio":
        return "bestaudio[ext=m4a]/bestaudio/best"
    if not has_ffmpeg:
        return "b/best"
    table = {
        "720": "bv*[height<=720]+ba/b[height<=720]/best",
        "1080": "bv*[height<=1080]+ba/b[height<=1080]/best",
        "best": "bv*+ba/b/best",
    }
    return table.get(quality, table["best"])


def is_soft_download_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "429" in text
        or "too many requests" in text
        or "unable to download video subtitles" in text
    )


VOSK_MODELS = {
    "pt": {
        "name": "vosk-model-small-pt-0.3",
        "url": "https://alphacephei.com/vosk/models/vosk-model-small-pt-0.3.zip",
    },
    "en": {
        "name": "vosk-model-small-en-us-0.15",
        "url": "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
    },
}
_VOSK_CACHE: dict[str, Any] = {}


def vosk_lang_key(language: str | None) -> str:
    lang = (language or "pt").lower()
    if lang.startswith("en"):
        return "en"
    return "pt"


def is_cookie_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "cookie database" in text or "cookiesfrombrowser" in text or "could not copy chrome" in text


def copy_even_if_locked(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for copier in (_copy_shutil, _copy_os_open, _copy_win32_shared, _copy_sqlite_immutable):
        try:
            copier(src, dst)
            if dst.exists() and dst.stat().st_size > 0:
                return
            errors.append(f"{copier.__name__}: arquivo vazio")
        except Exception as exc:
            errors.append(f"{copier.__name__}: {exc}")
    raise PermissionError(
        "O Chrome está com os cookies travados. Feche o Chrome ou desmarque o login. "
        + "; ".join(errors[:2])
    )


def _copy_shutil(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)


def _copy_os_open(src: Path, dst: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(src, flags)
    try:
        with open(dst, "wb") as out:
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    finally:
        os.close(fd)


def _copy_win32_shared(src: Path, dst: Path) -> None:
    dst.write_bytes(_read_win32_shared(src))


def _copy_sqlite_immutable(src: Path, dst: Path) -> None:
    posix = src.resolve().as_posix()
    uris = [
        f"file:{posix}?mode=ro&immutable=1",
        f"file:{posix}?mode=ro&nolock=1&immutable=1",
        f"file:///{posix}?mode=ro&immutable=1",
    ]
    last_error: Exception | None = None
    for uri in uris:
        try:
            src_conn = sqlite3.connect(uri, uri=True)
            break
        except Exception as exc:
            last_error = exc
            src_conn = None
    if src_conn is None:
        raise last_error or PermissionError("SQLite não abriu os cookies.")
    try:
        if dst.exists():
            dst.unlink()
        dest_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def _read_win32_shared(src: Path) -> bytes:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    GENERIC_READ = 0x80000000
    FILE_SHARE = 0x00000001 | 0x00000002 | 0x00000004
    handle = kernel32.CreateFileW(str(src), GENERIC_READ, FILE_SHARE, None, 3, 0x02000000 | 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        handle = kernel32.CreateFileW(str(src), GENERIC_READ, FILE_SHARE, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise PermissionError(f"Não consegui ler {src.name} (Chrome aberto).")
    try:
        chunks: list[bytes] = []
        buf = ctypes.create_string_buffer(1024 * 1024)
        read = wintypes.DWORD()
        while True:
            ok = kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None)
            if not ok or read.value == 0:
                break
            chunks.append(buf.raw[: read.value])
        if not chunks:
            raise PermissionError(f"Arquivo de cookies vazio ou travado: {src.name}")
        return b"".join(chunks)
    finally:
        kernel32.CloseHandle(handle)


_COOKIE_DIRS: list[tempfile.TemporaryDirectory] = []

_CHROMIUM_USER_DATA = {
    "chrome": ("Google", "Chrome", "User Data"),
    "edge": ("Microsoft", "Edge", "User Data"),
}


def _chromium_user_data(browser: str) -> Path:
    parts = _CHROMIUM_USER_DATA.get(browser)
    if not parts:
        raise UrlError(f"Navegador sem suporte a cookies: {browser}")
    root = Path(os.environ.get("LOCALAPPDATA", "")) / Path(*parts)
    if not root.is_dir():
        raise FileNotFoundError(f"Não achei o perfil do {browser}.")
    return root


def _shadow_chromium_profile(browser: str, dest: Path) -> Path:
    user_data = _chromium_user_data(browser)
    dest.mkdir(parents=True, exist_ok=True)
    local_state = user_data / "Local State"
    if local_state.exists():
        copy_even_if_locked(local_state, dest / "Local State")
    default = user_data / "Default"
    dest_profile = dest / "Default"
    copied = False
    for relative in (Path("Network") / "Cookies", Path("Cookies")):
        src = default / relative
        if not src.exists():
            continue
        copy_even_if_locked(src, dest_profile / relative)
        for extra in ("-journal", "-wal"):
            sibling = Path(str(src) + extra)
            if sibling.exists():
                copy_even_if_locked(sibling, Path(str(dest_profile / relative) + extra))
        copied = True
    if not copied:
        raise FileNotFoundError(f"Não achei o banco de cookies do {browser}.")
    return dest_profile


def prepare_cookiefile(browser: str | None) -> str | None:
    if not browser:
        return None
    if browser == "firefox":
        return None
    tmp = tempfile.TemporaryDirectory(prefix="yt_cookies_")
    _COOKIE_DIRS.append(tmp)
    root = Path(tmp.name)
    profile = _shadow_chromium_profile(browser, root / "profile")
    cookiefile = root / "cookies.txt"
    with _ydl().YoutubeDL(
        {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "skip_download": True,
            "cookiesfrombrowser": (browser, str(profile)),
        }
    ) as ydl:
        ydl.cookiejar.save(str(cookiefile), ignore_discard=True, ignore_expires=True)
    if cookiefile.stat().st_size < 20:
        raise RuntimeError("Exportação de cookies veio vazia.")
    return str(cookiefile)


def _ydl():
    import yt_dlp

    return yt_dlp


def inspect_url(url: str) -> dict[str, Any]:
    parsed = parse_youtube_url(url)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
        "noplaylist": parsed["kind"] == "video",
    }
    with _ydl().YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise UrlError("O YouTube não devolveu dados desse link.")

    entries_raw = info.get("entries")
    if entries_raw:
        videos = []
        for index, entry in enumerate(entries_raw, start=1):
            if not entry:
                continue
            videos.append(
                {
                    "id": entry.get("id") or entry.get("url") or str(index),
                    "title": entry.get("title") or f"Vídeo {index}",
                    "duration": entry.get("duration"),
                    "url": entry.get("url")
                    or entry.get("webpage_url")
                    or f"https://www.youtube.com/watch?v={entry.get('id')}",
                    "index": index,
                }
            )
        title = info.get("title") or "Playlist"
        return {
            "kind": "playlist",
            "title": title,
            "id": info.get("id") or parsed["playlist_id"],
            "count": len(videos),
            "videos": videos,
        }

    video = {
        "id": info.get("id"),
        "title": info.get("title") or "Vídeo",
        "duration": info.get("duration"),
        "url": info.get("webpage_url") or url,
        "index": 1,
    }
    return {
        "kind": "video",
        "title": video["title"],
        "id": video["id"],
        "count": 1,
        "videos": [video],
    }


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_LEGENDA_EXTS = (".pt.vtt", ".pt-BR.vtt", ".en.vtt", ".vtt", ".pt.srt", ".pt-BR.srt", ".en.srt", ".srt")


def _sidecar_legenda(video_path: Path) -> Path | None:
    """Acha a legenda baixada junto do vídeo (nunca a .transcricao.srt gerada por nós)."""
    base = str(video_path.with_suffix(""))
    for ext in _LEGENDA_EXTS:
        alt = Path(base + ext)
        if alt.exists():
            return alt
    for path in sorted(video_path.parent.glob(f"{glob_escape(video_path.stem)}*")):
        if path.suffix.lower() in {".vtt", ".srt"} and ".transcricao." not in path.name:
            return path
    return None


def glob_escape(value: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", value)


def _sidecar_transcript(video_path: Path) -> str | None:
    """Texto corrido da legenda baixada (mantido por compatibilidade)."""
    path = _sidecar_legenda(video_path)
    if not path:
        return None
    raw = path.read_text(encoding="utf-8", errors="ignore")
    return vtt_to_text(raw) if path.suffix.lower() == ".vtt" else raw


def _segmentos_legenda(video_path: Path) -> list[transcricao.Segmento]:
    path = _sidecar_legenda(video_path)
    if not path:
        return []
    return transcricao.parse_legenda(path.read_text(encoding="utf-8", errors="ignore"))


def _youtube_segmentos(video_id: str, languages: list[str]) -> list[transcricao.Segmento]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception:
        return []
    langs = languages + [lang.split("-")[0] for lang in languages]
    langs = list(dict.fromkeys(langs + ["pt", "pt-BR", "en"]))
    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=langs)
        segs = transcricao.segmentos_de_snippets(fetched)
        if segs:
            return segs
    except Exception:
        pass
    try:
        snippets = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
        return transcricao.segmentos_de_snippets(snippets)
    except Exception:
        return []


def _youtube_transcript(video_id: str, languages: list[str]) -> str | None:
    segs = _youtube_segmentos(video_id, languages)
    return "\n".join(s.texto for s in segs) if segs else None


def _extract_wav(video_path: Path) -> Path:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg é necessário para transcrever o áudio do vídeo.")
    wav = video_path.with_name(video_path.stem + ".16k.wav")
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        str(wav),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not wav.exists():
        err = (result.stderr or result.stdout or "ffmpeg falhou").strip().splitlines()
        raise RuntimeError(err[-1] if err else "ffmpeg falhou ao extrair o áudio.")
    return wav


def ensure_vosk_model(language: str | None) -> Path:
    spec = VOSK_MODELS[vosk_lang_key(language)]
    dest = MODELS / spec["name"]
    if dest.is_dir() and ((dest / "am").exists() or (dest / "conf").exists() or (dest / "final.mdl").exists()):
        return dest
    MODELS.mkdir(parents=True, exist_ok=True)
    zip_path = MODELS / f"{spec['name']}.zip"
    if not zip_path.exists():
        urlretrieve(spec["url"], zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(MODELS)
    if dest.is_dir() and ((dest / "am").exists() or (dest / "conf").exists() or (dest / "final.mdl").exists()):
        zip_path.unlink(missing_ok=True)
        return dest
    extracted = [p for p in MODELS.iterdir() if p.is_dir() and spec["name"] in p.name]
    if extracted:
        zip_path.unlink(missing_ok=True)
        return extracted[0]
    raise RuntimeError(f"Modelo Vosk não encontrado em {MODELS}")


def _vosk_model(language: str | None):
    from vosk import Model

    key = vosk_lang_key(language)
    cached = _VOSK_CACHE.get(key)
    if cached:
        return cached
    path = ensure_vosk_model(language)
    model = Model(str(path))
    _VOSK_CACHE[key] = model
    return model


def _vosk_segmentos(video_path: Path, language: str | None) -> list[transcricao.Segmento]:
    try:
        from vosk import KaldiRecognizer, SetLogLevel
    except Exception:
        return []
    SetLogLevel(-1)
    wav = _extract_wav(video_path)
    try:
        import wave

        model = _vosk_model(language)
        resultados: list[dict[str, Any]] = []
        with wave.open(str(wav), "rb") as wf:
            rec = KaldiRecognizer(model, wf.getframerate())
            rec.SetWords(True)
            while True:
                data = wf.readframes(4000)
                if not data:
                    break
                if rec.AcceptWaveform(data):
                    resultados.append(json.loads(rec.Result()))
            resultados.append(json.loads(rec.FinalResult()))
        return transcricao.segmentos_de_vosk(resultados)
    finally:
        for attempt in range(5):
            try:
                wav.unlink(missing_ok=True)
                break
            except OSError:
                if attempt == 4:
                    break
                time.sleep(0.3)


def _vosk_transcript(video_path: Path, language: str | None) -> str | None:
    segs = _vosk_segmentos(video_path, language)
    return " ".join(s.texto for s in segs) or None


MOTORES = {
    # auto: legenda já baixada → Whisper local → Vosk → legendas do YouTube
    "auto": ("legenda", "whisper", "vosk", "youtube"),
    # rapido: só legendas (baixada ou do YouTube), sem processar o áudio
    "rapido": ("legenda", "youtube"),
    # whisper: força o Whisper mesmo havendo legenda
    "whisper": ("whisper",),
}


def video_id_do_nome(path: Path) -> str | None:
    match = re.search(r"\[([A-Za-z0-9_-]{11})\]", path.name)
    return match.group(1) if match else None


def transcribe_file(
    video_path: Path,
    video_id: str | None,
    languages: list[str] | None = None,
    engine: str = "auto",
    progress: Callable[[float], None] | None = None,
) -> tuple[Path, str]:
    """Transcreve com tempos. Gera .transcricao.json, .transcricao.srt e .txt.

    Devolve (caminho do .txt, fonte usada).
    """
    langs = languages or ["pt", "pt-BR", "en"]
    language = langs[0] if langs else "pt"
    video_id = video_id or video_id_do_nome(video_path)
    ordem = MOTORES.get(engine, MOTORES["auto"])
    erros: list[str] = []
    segmentos: list[transcricao.Segmento] = []
    fonte = ""
    for motor in ordem:
        try:
            if motor == "legenda":
                segmentos = _segmentos_legenda(video_path)
            elif motor == "whisper":
                if not transcricao.whisper_disponivel():
                    erros.append("whisper: faster-whisper não instalado")
                    continue
                segmentos = transcricao.transcrever_whisper(video_path, language, progress)
            elif motor == "vosk":
                segmentos = _vosk_segmentos(video_path, language)
            elif motor == "youtube" and video_id:
                segmentos = _youtube_segmentos(video_id, langs)
        except Exception as exc:
            erros.append(f"{motor}: {exc}")
            segmentos = []
        if segmentos:
            fonte = motor
            break
    if not segmentos:
        detalhe = "; ".join(erros) if erros else "nenhum motor devolveu texto"
        raise RuntimeError(f"Não consegui transcrever ({detalhe}).")
    saidas = transcricao.salvar(video_path, segmentos, fonte, language)
    _atualizar_live_json(video_path, saidas, fonte)
    return saidas["txt"], fonte


@dataclass
class Job:
    id: str
    title: str
    status: str = "queued"
    message: str = "Na fila"
    total: int = 0
    done: int = 0
    current: str = ""
    percent: float = 0.0
    errors: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    folder: str = ""
    _listeners: list[Queue] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "message": self.message,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "percent": round(self.percent, 1),
            "errors": list(self.errors),
            "files": list(self.files),
            "folder": self.folder,
            "finished": self.status in {"done", "error"},
        }

    def emit(self, extra: dict[str, Any] | None = None) -> None:
        payload = self.snapshot()
        if extra:
            payload.update(extra)
        for listener in list(self._listeners):
            listener.put(payload)

    def subscribe(self) -> Queue:
        queue: Queue = Queue()
        self._listeners.append(queue)
        queue.put(self.snapshot())
        return queue


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

LIVES = DOWNLOADS / "lives"
MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".m4a", ".mp3"}


def get_job(job_id: str) -> Job | None:
    return JOBS.get(job_id)


def _rel(path: Path) -> str:
    return str(path.relative_to(DOWNLOADS)).replace("\\", "/")


def _kind(path: Path) -> str | None:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name == "live.json" or name.endswith(".transcricao.json"):
        return "dados"
    if name.endswith(".transcricao.srt"):
        return "transcript"
    if suffix == ".txt":
        return "transcript"
    if suffix in {".vtt", ".srt"}:
        return "subtitle"
    if suffix in MEDIA_EXTS:
        return "video"
    return None


def list_library() -> list[dict[str, Any]]:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for path in sorted(DOWNLOADS.rglob("*")):
        if not path.is_file() or path.name == ".gitkeep":
            continue
        kind = _kind(path)
        if not kind:
            continue
        rel = path.relative_to(DOWNLOADS)
        items.append(
            {
                "name": path.name,
                "rel": str(rel).replace("\\", "/"),
                "size": path.stat().st_size,
                "kind": kind,
                "folder": str(rel.parent).replace("\\", "/"),
            }
        )
    return items


# --- metadados da live -----------------------------------------------------


def _data_live(info: dict[str, Any]) -> str:
    raw = str(info.get("release_date") or info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    ts = info.get("release_timestamp") or info.get("timestamp")
    if ts:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    return time.strftime("%Y-%m-%d")


def pasta_da_live(criador: dict[str, Any], info: dict[str, Any]) -> Path:
    """lives/AAAA-MM-DD_<criador>_<titulo>/ (formato do plano de cortes)."""
    nome = f"{_data_live(info)}_{safe_name(criador['nome'], 30)}_{safe_name(info.get('title') or 'live', 50)}"
    return LIVES / nome


def montar_live_json(
    info: dict[str, Any],
    criador: dict[str, Any],
    autorizacao: dict[str, Any],
    arquivo: Path | None,
    pasta: Path,
) -> dict[str, Any]:
    return {
        "versao": 1,
        "video_id": info.get("id"),
        "titulo": info.get("title"),
        "url": info.get("webpage_url") or info.get("original_url"),
        "plataforma": (info.get("extractor_key") or "Youtube").lower(),
        "canal": info.get("channel") or info.get("uploader"),
        "canal_id": info.get("channel_id"),
        "canal_url": info.get("channel_url") or info.get("uploader_url"),
        "data_live": _data_live(info),
        "duracao_seg": info.get("duration"),
        "foi_live": bool(info.get("was_live")) or info.get("live_status") in {"was_live", "post_live"},
        "baixado_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "arquivo": str(arquivo.relative_to(pasta)).replace("\\", "/") if arquivo else None,
        "criador": {"id": criador["id"], "nome": criador["nome"], "canais": criador.get("canais", [])},
        "autorizacao": {
            key: autorizacao.get(key)
            for key in (
                "id",
                "tipo",
                "programa",
                "prova",
                "regras",
                "plataformas",
                "pode_monetizar",
                "credito",
                "valido_de",
                "valido_ate",
            )
        },
        "transcricao": None,
    }


def _escrever_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _atualizar_live_json(media: Path, saidas: dict[str, Path], fonte: str) -> None:
    """Se o vídeo está em lives/<live>/bruto/, registra a transcrição no live.json."""
    if media.parent.name != "bruto":
        return
    live_json = media.parent.parent / "live.json"
    if not live_json.exists():
        return
    data = json.loads(live_json.read_text(encoding="utf-8"))
    pasta = live_json.parent
    data["transcricao"] = {
        "fonte": fonte,
        "gerada_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **{k: str(v.relative_to(pasta)).replace("\\", "/") for k, v in saidas.items()},
    }
    _escrever_json(live_json, data)


# --- jobs ------------------------------------------------------------------


def start_job(
    url: str,
    video_ids: list[str] | None,
    quality: str,
    transcribe: bool,
    cookies_browser: str | None,
    languages: list[str],
    subtitles: bool = False,
    creator_id: int | None = None,
    engine: str = "auto",
) -> Job:
    criador = autorizacao = None
    if creator_id is not None:
        # Bloqueio do plano: sem autorização válida, a live não entra.
        criador, autorizacao = autorizacoes.autorizacao_vigente(creator_id)

    inspected = inspect_url(url)
    selected = inspected["videos"]
    if video_ids:
        wanted = set(video_ids)
        selected = [item for item in inspected["videos"] if item["id"] in wanted]
    if not selected:
        raise UrlError("Nenhum vídeo selecionado.")

    if criador:
        folder = LIVES
    else:
        suffix = inspected.get("id") or "video"
        folder = DOWNLOADS / f"{safe_name(inspected['title'], 50)} {suffix[-8:]}"
    folder.mkdir(parents=True, exist_ok=True)
    job = Job(
        id=uuid.uuid4().hex[:10],
        title=inspected["title"],
        total=len(selected),
        folder=str(folder),
        message="Preparando download",
    )
    with JOBS_LOCK:
        JOBS[job.id] = job
    thread = threading.Thread(
        target=_run_job,
        kwargs={
            "job": job,
            "videos": selected,
            "quality": quality,
            "transcribe": transcribe,
            "subtitles": subtitles,
            "cookies_browser": cookies_browser,
            "languages": languages,
            "folder": folder,
            "criador": criador,
            "autorizacao": autorizacao,
            "engine": engine,
        },
        daemon=True,
    )
    thread.start()
    return job


def transcribe_existing(rels: list[str], languages: list[str], engine: str = "auto") -> Job:
    paths = []
    for rel in rels:
        candidate = (DOWNLOADS / rel).resolve()
        if DOWNLOADS.resolve() not in candidate.parents and candidate != DOWNLOADS.resolve():
            raise UrlError("Caminho inválido.")
        if candidate.suffix.lower() in MEDIA_EXTS and candidate.exists():
            paths.append(candidate)
    if not paths:
        raise UrlError("Nenhum vídeo para transcrever.")
    job = Job(
        id=uuid.uuid4().hex[:10],
        title="Transcrição",
        total=len(paths),
        folder=str(paths[0].parent),
        message="Transcrevendo arquivos já baixados",
    )
    with JOBS_LOCK:
        JOBS[job.id] = job
    thread = threading.Thread(
        target=_run_transcribe_only,
        kwargs={"job": job, "paths": paths, "languages": languages, "engine": engine},
        daemon=True,
    )
    thread.start()
    return job


OUTTMPL_ARQUIVO = "%(playlist_index)03d - %(title).80B [%(id)s].%(ext)s"
OUTTMPL_LIVE = "%(title).80B [%(id)s].%(ext)s"


def _base_opts(
    folder: Path,
    quality: str,
    job: Job,
    cookiefile: str | None = None,
    cookies_browser: str | None = None,
    subtitles: bool = False,
    languages: list[str] | None = None,
    outtmpl: str = OUTTMPL_ARQUIVO,
) -> dict[str, Any]:
    ffmpeg = ffmpeg_path()
    has_ffmpeg = bool(ffmpeg)
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "noprogress": True,
        "outtmpl": str(folder / outtmpl),
        "restrictfilenames": False,
        "windowsfilenames": True,
        "format": format_selector(quality, has_ffmpeg),
        "merge_output_format": "mp4" if quality != "audio" else None,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 3,
        "overwrites": False,
        "sleep_interval_requests": 1.0,
        "progress_hooks": [lambda data, j=job: _progress_hook(j, data)],
    }
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    if quality == "audio":
        opts.pop("merge_output_format", None)
    if subtitles:
        langs = list(dict.fromkeys((languages or []) + ["pt", "pt-BR", "en"]))
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = langs
        opts["sleep_interval_subtitles"] = 2.0
    if cookiefile:
        opts["cookiefile"] = cookiefile
    elif cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    return {k: v for k, v in opts.items() if v is not None}


def _ydl_download(opts: dict[str, Any], target: str) -> None:
    with _ydl().YoutubeDL(opts) as ydl:
        ydl.download([target])


def _ydl_info(opts: dict[str, Any], target: str) -> dict[str, Any]:
    info_opts = {k: v for k, v in opts.items() if k not in {"progress_hooks"}}
    info_opts["skip_download"] = True
    with _ydl().YoutubeDL(info_opts) as ydl:
        info = ydl.extract_info(target, download=False)
    if not info:
        raise RuntimeError("O YouTube não devolveu dados do vídeo.")
    return ydl.sanitize_info(info) if hasattr(ydl, "sanitize_info") else info


def _progress_hook(job: Job, data: dict[str, Any]) -> None:
    status = data.get("status")
    if status == "downloading":
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        downloaded = data.get("downloaded_bytes") or 0
        piece = (downloaded / total * 100) if total else 0
        job.percent = ((job.done + piece / 100) / max(job.total, 1)) * 100
        speed = data.get("_speed_str") or ""
        eta = data.get("_eta_str") or ""
        job.message = f"Baixando {job.current} {speed} {eta}".strip()
        job.emit()
    elif status == "finished":
        job.message = f"Processando {job.current}"
        job.emit()


def _skip(job: Job, erro: str) -> None:
    job.errors.append(erro)
    job.done += 1
    job.percent = (job.done / job.total) * 100
    job.emit()


def _transcribe_progress(job: Job, titulo: str) -> Callable[[float], None]:
    ultimo = {"t": 0.0}

    def _cb(frac: float) -> None:
        agora = time.time()
        if agora - ultimo["t"] < 2:
            return
        ultimo["t"] = agora
        job.message = f"Transcrevendo {titulo} ({frac * 100:.0f}%)"
        job.emit()

    return _cb


def _run_job(
    job: Job,
    videos: list[dict[str, Any]],
    quality: str,
    transcribe: bool,
    cookies_browser: str | None,
    languages: list[str],
    folder: Path,
    subtitles: bool = False,
    criador: dict[str, Any] | None = None,
    autorizacao: dict[str, Any] | None = None,
    engine: str = "auto",
) -> None:
    job.status = "running"
    job.emit()
    cookiefile = None
    live_browser = None
    if cookies_browser == "firefox":
        live_browser = "firefox"
    elif cookies_browser:
        job.message = "Lendo cookies do navegador (Chrome pode ficar aberto)"
        job.emit()
        try:
            cookiefile = prepare_cookiefile(cookies_browser)
        except Exception as exc:
            job.errors.append(
                f"Cookies do {cookies_browser}: {exc}. Seguindo sem login."
            )
    outtmpl = OUTTMPL_LIVE if criador else OUTTMPL_ARQUIVO
    try:
        for video in videos:
            if job._stop.is_set():
                break
            job.current = video["title"]
            target = video.get("url") or f"https://www.youtube.com/watch?v={video['id']}"
            dest = folder
            info: dict[str, Any] | None = None
            live_dir: Path | None = None
            if criador:
                job.message = f"Conferindo canal de {video['title']}"
                job.emit()
                try:
                    info = _ydl_info(
                        _base_opts(folder, quality, job, cookiefile=cookiefile, cookies_browser=live_browser),
                        target,
                    )
                except Exception as exc:
                    _skip(job, f"{video['title']}: não consegui ler os dados ({exc})")
                    continue
                if not autorizacoes.canal_confere(criador, info):
                    canal = info.get("channel") or info.get("uploader") or "desconhecido"
                    _skip(
                        job,
                        f"{video['title']}: bloqueado. O canal \"{canal}\" não está cadastrado para {criador['nome']}.",
                    )
                    continue
                live_dir = pasta_da_live(criador, info)
                dest = live_dir / "bruto"
                dest.mkdir(parents=True, exist_ok=True)
            job.message = f"Baixando {video['title']}"
            job.emit()
            opts = _base_opts(
                dest,
                quality,
                job,
                cookiefile=cookiefile,
                cookies_browser=live_browser,
                subtitles=subtitles,
                languages=languages,
                outtmpl=outtmpl,
            )
            before = {p.resolve() for p in dest.glob("*") if p.is_file()}
            try:
                _ydl_download(opts, target)
            except Exception as exc:
                if is_cookie_lock_error(exc) or is_soft_download_error(exc):
                    job.message = f"Nova tentativa: {video['title']}"
                    job.emit()
                    time.sleep(2)
                    try:
                        _ydl_download(_base_opts(dest, quality, job, outtmpl=outtmpl), target)
                    except Exception as exc2:
                        _skip(job, f"{video['title']}: {exc2}")
                        continue
                else:
                    _skip(job, f"{video['title']}: {exc}")
                    continue
            after = [p for p in dest.glob("*") if p.is_file() and p.resolve() not in before]
            media = [p for p in after if p.suffix.lower() in MEDIA_EXTS]
            if not media:
                # Arquivo já existia (overwrites=False): usa o que tem o ID no nome.
                media = [p for p in dest.glob(f"*[[]{video['id']}[]]*") if p.suffix.lower() in MEDIA_EXTS]
            media.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            saved = media[0] if media else None
            if live_dir and info is not None and criador and autorizacao:
                _escrever_json(
                    live_dir / "live.json",
                    montar_live_json(info, criador, autorizacao, saved, live_dir),
                )
            transcript_rel = None
            if saved and transcribe:
                job.message = f"Transcrevendo {video['title']}"
                job.emit()
                try:
                    txt, source = transcribe_file(
                        saved,
                        video.get("id"),
                        languages,
                        engine=engine,
                        progress=_transcribe_progress(job, video["title"]),
                    )
                    transcript_rel = _rel(txt)
                    job.message = f"Transcrição ({source}): {txt.name}"
                except Exception as exc:
                    job.errors.append(f"Transcrição {video['title']}: {exc}")
            if saved:
                job.files.append(
                    {
                        "name": saved.name,
                        "rel": _rel(saved),
                        "transcript": transcript_rel,
                        "live": _rel(live_dir / "live.json") if live_dir else None,
                    }
                )
            job.done += 1
            job.percent = (job.done / job.total) * 100
            job.emit()
            time.sleep(1)
        job.status = "error" if job.errors and not job.files else "done"
        job.message = "Concluído" if not job.errors else "Concluído com erros"
        job.current = ""
        job.percent = 100
        job.emit({"done": True})
    except Exception as exc:
        job.status = "error"
        job.message = str(exc)
        job.errors.append(str(exc))
        job.emit({"done": True})


def _run_transcribe_only(job: Job, paths: list[Path], languages: list[str], engine: str = "auto") -> None:
    job.status = "running"
    job.emit()
    try:
        for path in paths:
            job.current = path.name
            job.message = f"Transcrevendo {path.name}"
            job.emit()
            try:
                txt, source = transcribe_file(
                    path,
                    video_id_do_nome(path),
                    languages,
                    engine=engine,
                    progress=_transcribe_progress(job, path.name),
                )
                job.files.append(
                    {
                        "name": txt.name,
                        "rel": _rel(txt),
                        "transcript": _rel(txt),
                        "source": source,
                    }
                )
            except Exception as exc:
                job.errors.append(f"{path.name}: {exc}")
            job.done += 1
            job.percent = (job.done / job.total) * 100
            job.emit()
        job.status = "error" if job.errors and not job.files else "done"
        job.message = "Transcrição concluída" if job.status == "done" else "Transcrição com erros"
        job.current = ""
        job.emit({"done": True})
    except Exception as exc:
        job.status = "error"
        job.message = str(exc)
        job.emit({"done": True})


def dump_jobs() -> list[dict[str, Any]]:
    return [job.snapshot() for job in JOBS.values()]
