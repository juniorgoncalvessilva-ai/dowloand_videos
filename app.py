"""Servidor local: baixar playlists do YouTube e transcrever."""

from __future__ import annotations

import json
from pathlib import Path
from queue import Empty

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import autorizacoes
import core

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
DOWNLOADS = core.DOWNLOADS

app = FastAPI(title="Arquivo YouTube", docs_url=None, redoc_url=None)
app.mount("/files", StaticFiles(directory=DOWNLOADS), name="files")


class InspectBody(BaseModel):
    url: str = Field(min_length=8)


class StartBody(BaseModel):
    url: str = Field(min_length=8)
    video_ids: list[str] = Field(default_factory=list)
    quality: str = "best"
    transcribe: bool = False
    subtitles: bool = False
    cookies_browser: str | None = None
    language: str = "pt"
    creator_id: int | None = None
    engine: str = "auto"


class TranscribeBody(BaseModel):
    rels: list[str]
    language: str = "pt"
    engine: str = "auto"


class CriadorBody(BaseModel):
    nome: str = Field(min_length=1)
    canais: list[str] = Field(default_factory=list)
    notas: str = ""


class AutorizacaoBody(BaseModel):
    tipo: str
    prova: str = Field(min_length=1)
    programa: str = ""
    regras: str = ""
    plataformas: list[str] = Field(default_factory=list)
    pode_monetizar: bool = False
    credito: str = ""
    valido_de: str | None = None
    valido_ate: str | None = None


def _engine(value: str) -> str:
    return value if value in core.MOTORES else "auto"


def _langs(language: str) -> list[str]:
    language = (language or "pt").strip() or "pt"
    if language == "auto":
        return ["pt", "pt-BR", "en"]
    return [language, "pt", "pt-BR", "en"]


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return HTMLResponse((TEMPLATES / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict:
    tools = core.which_tools()
    tools["ok"] = True
    return tools


@app.post("/api/inspect")
def inspect(body: InspectBody) -> dict:
    try:
        return core.inspect_url(body.url)
    except core.UrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha ao ler o YouTube: {exc}") from exc


@app.post("/api/start")
def start(body: StartBody) -> dict:
    cookies = body.cookies_browser if body.cookies_browser in {"chrome", "edge", "firefox"} else None
    try:
        job = core.start_job(
            url=body.url,
            video_ids=body.video_ids or None,
            quality=body.quality,
            transcribe=body.transcribe,
            subtitles=body.subtitles,
            cookies_browser=cookies,
            languages=_langs(body.language),
            creator_id=body.creator_id,
            engine=_engine(body.engine),
        )
        return job.snapshot()
    except autorizacoes.AutorizacaoError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except core.UrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/transcribe")
def transcribe(body: TranscribeBody) -> dict:
    try:
        job = core.transcribe_existing(body.rels, _langs(body.language), _engine(body.engine))
        return job.snapshot()
    except core.UrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/criadores")
def criadores() -> dict:
    return {
        "items": autorizacoes.listar_criadores(),
        "tipos": autorizacoes.TIPOS,
        "plataformas": list(autorizacoes.PLATAFORMAS),
    }


@app.post("/api/criadores")
def criar_criador(body: CriadorBody) -> dict:
    try:
        return autorizacoes.criar_criador(body.nome, body.canais, body.notas)
    except autorizacoes.AutorizacaoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/criadores/{criador_id}")
def obter_criador(criador_id: int) -> dict:
    criador = autorizacoes.obter_criador(criador_id)
    if not criador:
        raise HTTPException(status_code=404, detail="Criador não encontrado.")
    return criador


@app.post("/api/criadores/{criador_id}/autorizacoes")
def adicionar_autorizacao(criador_id: int, body: AutorizacaoBody) -> dict:
    try:
        return autorizacoes.adicionar_autorizacao(criador_id, **body.model_dump())
    except autorizacoes.AutorizacaoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/autorizacoes/{autorizacao_id}/revogar")
def revogar_autorizacao(autorizacao_id: int) -> dict:
    try:
        return autorizacoes.revogar_autorizacao(autorizacao_id)
    except autorizacoes.AutorizacaoError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/stream")
def job_stream(job_id: str) -> StreamingResponse:
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    queue = job.subscribe()

    def generate():
        try:
            while True:
                try:
                    payload = queue.get(timeout=15)
                except Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if payload.get("finished") or payload.get("done"):
                    break
        finally:
            if queue in job._listeners:
                job._listeners.remove(queue)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/library")
def library() -> dict:
    return {"items": core.list_library(), "root": str(DOWNLOADS)}


@app.get("/download")
def download_file(rel: str = Query(..., min_length=1)) -> FileResponse:
    path = (DOWNLOADS / rel).resolve()
    if DOWNLOADS.resolve() not in path.parents and path != DOWNLOADS.resolve():
        raise HTTPException(status_code=400, detail="Caminho inválido.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    return FileResponse(path, filename=path.name)
