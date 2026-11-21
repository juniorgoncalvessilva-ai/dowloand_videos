"""Transcrição com tempos: cada fala (segmento) tem início e fim, e as palavras
também, quando o motor informa.

Formato salvo em <video>.transcricao.json:

    {
      "versao": 1,
      "fonte": "whisper" | "legenda" | "vosk" | "youtube",
      "idioma": "pt",
      "arquivo": "video.mp4",
      "gerado_em": "...",
      "segmentos": [
        {"inicio": 12.34, "fim": 15.8, "texto": "...", "falante": null,
         "palavras": [{"inicio": 12.34, "fim": 12.6, "palavra": "olá"}]}
      ]
    }

"falante" fica nulo por enquanto (diarização entra numa etapa futura).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")
_INLINE = re.compile(r"<(\d+:\d{2}:\d{2}\.\d{3})>")
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Palavra:
    inicio: float
    fim: float
    palavra: str


@dataclass
class Segmento:
    inicio: float
    fim: float
    texto: str
    palavras: list[Palavra] = field(default_factory=list)
    falante: str | None = None


def _secs(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _clock(value: str) -> float:
    h, m, rest = value.split(":")
    s, ms = rest.split(".")
    return _secs(h, m, s, ms)


def fmt_ts(seconds: float, sep: str = ",") -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def fmt_relogio(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _clean(text: str) -> str:
    text = _TAG.sub("", text).replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _palavras_inline(line: str, inicio: float, fim: float) -> list[Palavra]:
    """Lê os tempos por palavra das legendas automáticas do YouTube:
    `palavra<00:00:01.234><c> outra</c>`."""
    partes = _INLINE.split(line)
    # partes = [texto0, ts1, texto1, ts2, texto2, ...]
    marcas: list[tuple[float, str]] = [(inicio, partes[0])]
    for i in range(1, len(partes) - 1, 2):
        marcas.append((_clock(partes[i]), partes[i + 1]))
    palavras: list[Palavra] = []
    for idx, (t, bruto) in enumerate(marcas):
        texto = _clean(bruto)
        if not texto:
            continue
        t_fim = marcas[idx + 1][0] if idx + 1 < len(marcas) else fim
        palavras.append(Palavra(round(t, 3), round(max(t, t_fim), 3), texto))
    return palavras


def parse_legenda(raw: str) -> list[Segmento]:
    """Converte VTT ou SRT em segmentos com tempo, sem repetir linhas.

    As legendas automáticas do YouTube repetem a linha anterior em cada cue
    (efeito de rolagem); aqui cada linha de texto entra uma vez só, com o tempo
    da primeira cue em que apareceu.
    """
    segmentos: list[Segmento] = []
    vistos_recentes: list[str] = []
    blocos = re.split(r"\n\s*\n", raw.replace("\r\n", "\n").replace("\r", "\n"))
    for bloco in blocos:
        linhas = bloco.strip("\n").split("\n")
        idx = next((i for i, l in enumerate(linhas) if _CUE.match(l)), None)
        if idx is None:
            continue
        m = _CUE.match(linhas[idx])
        inicio = _secs(*m.groups()[0:4])
        fim = _secs(*m.groups()[4:8])
        for linha in linhas[idx + 1:]:
            texto = _clean(linha)
            if not texto or texto in vistos_recentes:
                continue
            palavras = _palavras_inline(linha, inicio, fim) if _INLINE.search(linha) else []
            segmentos.append(Segmento(round(inicio, 3), round(fim, 3), texto, palavras))
            vistos_recentes = (vistos_recentes + [texto])[-3:]
    # Fecha o fim de cada segmento no início do próximo quando a legenda sobrepõe.
    for atual, prox in zip(segmentos, segmentos[1:]):
        if prox.inicio > atual.inicio and atual.fim > prox.inicio:
            atual.fim = prox.inicio
    return segmentos


def segmentos_de_snippets(snippets: Iterable[Any]) -> list[Segmento]:
    """Converte o retorno da youtube-transcript-api (objetos ou dicts)."""
    segs: list[Segmento] = []
    for item in snippets:
        get = item.get if isinstance(item, dict) else lambda k, d=None, o=item: getattr(o, k, d)
        texto = _clean(str(get("text", "") or ""))
        if not texto:
            continue
        inicio = float(get("start", 0) or 0)
        dur = float(get("duration", 0) or 0)
        segs.append(Segmento(round(inicio, 3), round(inicio + dur, 3), texto))
    return segs


def segmentos_de_vosk(resultados: Iterable[dict[str, Any]]) -> list[Segmento]:
    """Cada resultado do Vosk (com SetWords(True)) vira um segmento."""
    segs: list[Segmento] = []
    for res in resultados:
        words = res.get("result") or []
        texto = (res.get("text") or "").strip()
        if not words or not texto:
            continue
        palavras = [Palavra(round(w["start"], 3), round(w["end"], 3), w["word"]) for w in words]
        segs.append(Segmento(palavras[0].inicio, palavras[-1].fim, texto, palavras))
    return segs


# --- faster-whisper -------------------------------------------------------

_WHISPER_CACHE: dict[str, Any] = {}


def whisper_disponivel() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def _whisper_model():
    from faster_whisper import WhisperModel

    nome = os.environ.get("WHISPER_MODEL", "small")
    device = os.environ.get("WHISPER_DEVICE", "auto")
    compute = os.environ.get("WHISPER_COMPUTE", "int8" if device in ("auto", "cpu") else "float16")
    chave = f"{nome}|{device}|{compute}"
    if chave not in _WHISPER_CACHE:
        _WHISPER_CACHE[chave] = WhisperModel(nome, device=device, compute_type=compute)
    return _WHISPER_CACHE[chave]


def transcrever_whisper(media: Path, idioma: str | None, progresso=None) -> list[Segmento]:
    model = _whisper_model()
    lang = None if not idioma or idioma == "auto" else idioma.split("-")[0]
    segments, info = model.transcribe(
        str(media),
        language=lang,
        word_timestamps=True,
        vad_filter=True,
    )
    total = float(getattr(info, "duration", 0) or 0)
    segs: list[Segmento] = []
    for s in segments:
        palavras = [
            Palavra(round(w.start, 3), round(w.end, 3), w.word.strip())
            for w in (s.words or [])
            if w.word.strip()
        ]
        texto = s.text.strip()
        if texto:
            segs.append(Segmento(round(s.start, 3), round(s.end, 3), texto, palavras))
        if progresso and total:
            progresso(min(s.end / total, 1.0))
    return segs


# --- saída ----------------------------------------------------------------


def para_dict(segmentos: list[Segmento], fonte: str, idioma: str, arquivo: str) -> dict[str, Any]:
    return {
        "versao": 1,
        "fonte": fonte,
        "idioma": idioma,
        "arquivo": arquivo,
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "duracao_falada": round(sum(s.fim - s.inicio for s in segmentos), 1),
        "segmentos": [asdict(s) for s in segmentos],
    }


def para_srt(segmentos: list[Segmento]) -> str:
    blocos = []
    for i, s in enumerate(segmentos, start=1):
        blocos.append(f"{i}\n{fmt_ts(s.inicio)} --> {fmt_ts(s.fim)}\n{s.texto}\n")
    return "\n".join(blocos)


def para_txt(segmentos: list[Segmento], cabecalho: str) -> str:
    linhas = [f"[{fmt_relogio(s.inicio)}] {s.texto}" for s in segmentos]
    return cabecalho + "\n".join(linhas) + "\n"


def caminhos_saida(media: Path) -> dict[str, Path]:
    base = media.with_suffix("")
    return {
        "json": Path(f"{base}.transcricao.json"),
        "srt": Path(f"{base}.transcricao.srt"),
        "txt": media.with_suffix(".txt"),
    }


def salvar(media: Path, segmentos: list[Segmento], fonte: str, idioma: str) -> dict[str, Path]:
    saidas = caminhos_saida(media)
    dados = para_dict(segmentos, fonte, idioma, media.name)
    saidas["json"].write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    saidas["srt"].write_text(para_srt(segmentos), encoding="utf-8")
    cab = f"# Transcrição ({fonte})\n# Arquivo: {media.name}\n# Tempos em [hh:mm:ss] desde o início do vídeo\n\n"
    saidas["txt"].write_text(para_txt(segmentos, cab), encoding="utf-8")
    return saidas
