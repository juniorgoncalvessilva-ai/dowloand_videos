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
        "openai_key": False,
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


def _sidecar_transcript(video_path: Path) -> str | None:
    for ext in (".pt.vtt", ".pt-BR.vtt", ".en.vtt", ".vtt", ".pt.srt", ".srt"):
        candidate = video_path.with_suffix("")
        # yt-dlp names: file.pt.vtt next to file.mp4
        alt = Path(str(video_path.with_suffix("")) + ext)
        if alt.exists():
            raw = alt.read_text(encoding="utf-8", errors="ignore")
            return vtt_to_text(raw) if ext.endswith("vtt") else raw
        if candidate.with_suffix(ext).exists():
            raw = candidate.with_suffix(ext).read_text(encoding="utf-8", errors="ignore")
            return vtt_to_text(raw) if ext.endswith("vtt") else raw
    parent = video_path.parent
    stem = video_path.stem
    for path in parent.glob(f"{stem}*"):
        if path.suffix.lower() in {".vtt", ".srt"}:
            raw = path.read_text(encoding="utf-8", errors="ignore")
            return vtt_to_text(raw) if path.suffix.lower() == ".vtt" else raw
    return None


def _youtube_transcript(video_id: str, languages: list[str]) -> str | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception:
        return None
    langs = languages + [lang.split("-")[0] for lang in languages]
    langs = list(dict.fromkeys(langs + ["pt", "pt-BR", "en"]))
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=langs)
        parts = [item.text.strip() for item in fetched if getattr(item, "text", "").strip()]
        return "\n".join(parts) if parts else None
    except Exception:
        pass
    try:
        snippets = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
        parts = [item["text"].strip() for item in snippets if item.get("text")]
        return "\n".join(parts) if parts else None
    except Exception:
        return None


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


def _vosk_transcript(video_path: Path, language: str | None) -> str | None:
    try:
        from vosk import KaldiRecognizer, SetLogLevel
    except Exception:
        return None
    SetLogLevel(-1)
    wav = _extract_wav(video_path)
    try:
        import wave

        model = _vosk_model(language)
        with wave.open(str(wav), "rb") as wf:
            rec = KaldiRecognizer(model, wf.getframerate())
            rec.SetWords(False)
            chunks: list[str] = []
            while True:
                data = wf.readframes(4000)
                if not data:
                    break
                if rec.AcceptWaveform(data):
                    piece = json.loads(rec.Result()).get("text", "").strip()
                    if piece:
                        chunks.append(piece)
            final = json.loads(rec.FinalResult()).get("text", "").strip()
            if final:
                chunks.append(final)
        text = " ".join(chunks).strip()
        return text or None
    finally:
        for attempt in range(5):
            try:
                wav.unlink(missing_ok=True)
                break
            except OSError:
                if attempt == 4:
                    break
                time.sleep(0.3)


def transcribe_file(
    video_path: Path,
    video_id: str | None,
    languages: list[str] | None = None,
) -> tuple[Path, str]:
    langs = languages or ["pt", "pt-BR", "en"]
    language = langs[0] if langs else "pt"
    text = _sidecar_transcript(video_path)
    source = "legenda"
    if not text:
        text = _vosk_transcript(video_path, language)
        source = "vosk"
    if not text and video_id:
        text = _youtube_transcript(video_id, langs)
        source = "youtube"
    if not text:
        raise RuntimeError(
            "A transcrição local (Vosk) falhou. Confira o FFmpeg e o modelo em models/."
        )
    out = video_path.with_suffix(".txt")
    header = f"# Transcrição ({source})\n# Arquivo: {video_path.name}\n\n"
    _write_text(out, header + text + "\n")
    return out, source


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


def get_job(job_id: str) -> Job | None:
    return JOBS.get(job_id)


def list_library() -> list[dict[str, Any]]:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for path in sorted(DOWNLOADS.rglob("*")):
        if not path.is_file() or path.name == ".gitkeep":
            continue
        if path.suffix.lower() not in {".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".txt", ".vtt", ".srt"}:
            continue
        rel = path.relative_to(DOWNLOADS)
        items.append(
            {
                "name": path.name,
                "rel": str(rel).replace("\\", "/"),
                "size": path.stat().st_size,
                "kind": "transcript" if path.suffix.lower() == ".txt" else "subtitle" if path.suffix.lower() in {".vtt", ".srt"} else "video",
                "folder": str(rel.parent).replace("\\", "/"),
            }
        )
    return items


def start_job(
    url: str,
    video_ids: list[str] | None,
    quality: str,
    transcribe: bool,
    cookies_browser: str | None,
    languages: list[str],
    subtitles: bool = False,
) -> Job:
    inspected = inspect_url(url)
    selected = inspected["videos"]
    if video_ids:
        wanted = set(video_ids)
        selected = [item for item in inspected["videos"] if item["id"] in wanted]
    if not selected:
        raise UrlError("Nenhum vídeo selecionado.")

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
        },
        daemon=True,
    )
    thread.start()
    return job


def transcribe_existing(rels: list[str], languages: list[str]) -> Job:
    paths = []
    for rel in rels:
        candidate = (DOWNLOADS / rel).resolve()
        if DOWNLOADS.resolve() not in candidate.parents and candidate != DOWNLOADS.resolve():
            raise UrlError("Caminho inválido.")
        if candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".m4a", ".mp3"} and candidate.exists():
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
        kwargs={"job": job, "paths": paths, "languages": languages},
        daemon=True,
    )
    thread.start()
    return job


def _base_opts(
    folder: Path,
    quality: str,
    job: Job,
    cookiefile: str | None = None,
    cookies_browser: str | None = None,
    subtitles: bool = False,
    languages: list[str] | None = None,
) -> dict[str, Any]:
    ffmpeg = ffmpeg_path()
    has_ffmpeg = bool(ffmpeg)
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "noprogress": True,
        "outtmpl": str(folder / "%(playlist_index)03d - %(title).80B.%(ext)s"),
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


def _run_job(
    job: Job,
    videos: list[dict[str, Any]],
    quality: str,
    transcribe: bool,
    cookies_browser: str | None,
    languages: list[str],
    folder: Path,
    subtitles: bool = False,
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
    try:
        for video in videos:
            if job._stop.is_set():
                break
            job.current = video["title"]
            job.message = f"Baixando {video['title']}"
            job.emit()
            opts = _base_opts(
                folder,
                quality,
                job,
                cookiefile=cookiefile,
                cookies_browser=live_browser,
                subtitles=subtitles,
                languages=languages,
            )
            target = video.get("url") or f"https://www.youtube.com/watch?v={video['id']}"
            before = {p.resolve() for p in folder.glob("*") if p.is_file()}
            try:
                _ydl_download(opts, target)
            except Exception as exc:
                if is_cookie_lock_error(exc) or is_soft_download_error(exc):
                    job.message = f"Nova tentativa: {video['title']}"
                    job.emit()
                    time.sleep(2)
                    try:
                        _ydl_download(_base_opts(folder, quality, job), target)
                    except Exception as exc2:
                        job.errors.append(f"{video['title']}: {exc2}")
                        job.done += 1
                        job.percent = (job.done / job.total) * 100
                        job.emit()
                        continue
                else:
                    job.errors.append(f"{video['title']}: {exc}")
                    job.done += 1
                    job.percent = (job.done / job.total) * 100
                    job.emit()
                    continue
            after = [p for p in folder.glob("*") if p.is_file() and p.resolve() not in before]
            media = [
                p
                for p in after
                if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".m4a", ".mp3"}
            ]
            media.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            saved = media[0] if media else None
            transcript_rel = None
            if saved and transcribe:
                job.message = f"Transcrevendo {video['title']}"
                job.emit()
                try:
                    txt, source = transcribe_file(saved, video.get("id"), languages)
                    transcript_rel = str(txt.relative_to(DOWNLOADS)).replace("\\", "/")
                    job.message = f"Transcrição ({source}): {txt.name}"
                except Exception as exc:
                    job.errors.append(f"Transcrição {video['title']}: {exc}")
            if saved:
                job.files.append(
                    {
                        "name": saved.name,
                        "rel": str(saved.relative_to(DOWNLOADS)).replace("\\", "/"),
                        "transcript": transcript_rel,
                    }
                )
            job.done += 1
            job.percent = (job.done / job.total) * 100
            job.emit()
            time.sleep(1)
        job.status = "error" if job.errors and job.done == len(job.errors) else "done"
        job.message = "Concluído" if job.status == "done" else "Concluído com erros"
        job.current = ""
        job.percent = 100
        job.emit({"done": True})
    except Exception as exc:
        job.status = "error"
        job.message = str(exc)
        job.errors.append(str(exc))
        job.emit({"done": True})


def _run_transcribe_only(job: Job, paths: list[Path], languages: list[str]) -> None:
    job.status = "running"
    job.emit()
    try:
        for path in paths:
            job.current = path.name
            job.message = f"Transcrevendo {path.name}"
            job.emit()
            video_id = None
            match = re.search(r"\[([A-Za-z0-9_-]{11})\]", path.name)
            if match:
                video_id = match.group(1)
            try:
                txt, source = transcribe_file(path, video_id, languages)
                job.files.append(
                    {
                        "name": txt.name,
                        "rel": str(txt.relative_to(DOWNLOADS)).replace("\\", "/"),
                        "transcript": str(txt.relative_to(DOWNLOADS)).replace("\\", "/"),
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
