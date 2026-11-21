"""Cadastro de criadores e das autorizações de corte de cada um.

Regra do projeto: só entra no pipeline de cortes a live de um criador com
autorização válida (programa oficial de cortes, contrato ou licença aberta).
Tudo fica num SQLite local em data/cortes.db.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "cortes.db"

TIPOS = {
    "programa": "Programa oficial de cortes",
    "contrato": "Contrato direto com o criador",
    "licenca": "Licença aberta (ex.: Creative Commons)",
}
PLATAFORMAS = ("youtube", "tiktok", "instagram", "kwai", "facebook")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS criadores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    canais TEXT NOT NULL DEFAULT '[]',
    notas TEXT NOT NULL DEFAULT '',
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS autorizacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    criador_id INTEGER NOT NULL REFERENCES criadores(id),
    tipo TEXT NOT NULL,
    programa TEXT NOT NULL DEFAULT '',
    prova TEXT NOT NULL,
    regras TEXT NOT NULL DEFAULT '',
    plataformas TEXT NOT NULL DEFAULT '[]',
    pode_monetizar INTEGER NOT NULL DEFAULT 0,
    credito TEXT NOT NULL DEFAULT '',
    valido_de TEXT,
    valido_ate TEXT,
    revogada_em TEXT,
    criado_em TEXT NOT NULL
);
"""

_LOCK = threading.Lock()


class AutorizacaoError(ValueError):
    """Dado inválido ou live sem autorização."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def _conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()


def normalizar_canal(valor: str) -> str:
    """Reduz um link ou id de canal a uma chave comparável.

    Aceita "UCxxxx", "@handle", "https://www.youtube.com/@handle",
    "https://www.youtube.com/channel/UCxxxx" e "https://twitch.tv/nome".
    """
    raw = (valor or "").strip()
    if not raw:
        return ""
    lowered = raw.lower().rstrip("/")
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):]
    for host in ("www.", "m."):
        if lowered.startswith(host):
            lowered = lowered[len(host):]
    if lowered.startswith("youtube.com/"):
        rest = lowered[len("youtube.com/"):]
        if rest.startswith("channel/"):
            return rest[len("channel/"):].split("/")[0]
        if rest.startswith("@"):
            return rest.split("/")[0]
        if rest.startswith(("c/", "user/")):
            return "@" + rest.split("/")[1]
        return rest.split("/")[0]
    if lowered.startswith("twitch.tv/"):
        return "twitch:" + lowered[len("twitch.tv/"):].split("/")[0]
    if lowered.startswith("kick.com/"):
        return "kick:" + lowered[len("kick.com/"):].split("/")[0]
    return lowered


def _parse_date(value: str | None, campo: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise AutorizacaoError(f"Data inválida em {campo}: use AAAA-MM-DD.") from exc


def _criador_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["canais"] = json.loads(data.get("canais") or "[]")
    return data


def _autorizacao_dict(row: sqlite3.Row, hoje: date | None = None) -> dict[str, Any]:
    data = dict(row)
    data["plataformas"] = json.loads(data.get("plataformas") or "[]")
    data["pode_monetizar"] = bool(data.get("pode_monetizar"))
    data["tipo_nome"] = TIPOS.get(data["tipo"], data["tipo"])
    data["valida"] = autorizacao_valida(data, hoje)
    return data


def autorizacao_valida(aut: dict[str, Any], hoje: date | None = None) -> bool:
    hoje = hoje or date.today()
    if aut.get("revogada_em"):
        return False
    if aut.get("valido_de") and date.fromisoformat(aut["valido_de"]) > hoje:
        return False
    if aut.get("valido_ate") and date.fromisoformat(aut["valido_ate"]) < hoje:
        return False
    return True


def criar_criador(
    nome: str,
    canais: list[str] | None = None,
    notas: str = "",
    db_path: Path | None = None,
) -> dict[str, Any]:
    nome = (nome or "").strip()
    if not nome:
        raise AutorizacaoError("Informe o nome do criador.")
    canais_norm = [c for c in (normalizar_canal(x) for x in canais or []) if c]
    with _conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO criadores (nome, canais, notas, criado_em) VALUES (?, ?, ?, ?)",
            (nome, json.dumps(canais_norm), notas.strip(), _now()),
        )
        row = conn.execute("SELECT * FROM criadores WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _criador_dict(row)


def adicionar_autorizacao(
    criador_id: int,
    tipo: str,
    prova: str,
    programa: str = "",
    regras: str = "",
    plataformas: list[str] | None = None,
    pode_monetizar: bool = False,
    credito: str = "",
    valido_de: str | None = None,
    valido_ate: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    if tipo not in TIPOS:
        raise AutorizacaoError(f"Tipo de autorização inválido. Use: {', '.join(TIPOS)}.")
    prova = (prova or "").strip()
    if not prova:
        raise AutorizacaoError("Informe a prova da autorização (link do programa, contrato ou print).")
    plats = [p.strip().lower() for p in plataformas or [] if p and p.strip()]
    invalidas = [p for p in plats if p not in PLATAFORMAS]
    if invalidas:
        raise AutorizacaoError(f"Plataforma desconhecida: {', '.join(invalidas)}.")
    de = _parse_date(valido_de, "válido de")
    ate = _parse_date(valido_ate, "válido até")
    if de and ate and ate < de:
        raise AutorizacaoError("A data final é anterior à inicial.")
    with _conn(db_path) as conn:
        if not conn.execute("SELECT 1 FROM criadores WHERE id = ?", (criador_id,)).fetchone():
            raise AutorizacaoError("Criador não encontrado.")
        cur = conn.execute(
            """INSERT INTO autorizacoes
               (criador_id, tipo, programa, prova, regras, plataformas, pode_monetizar,
                credito, valido_de, valido_ate, criado_em)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                criador_id,
                tipo,
                programa.strip(),
                prova,
                regras.strip(),
                json.dumps(plats),
                int(bool(pode_monetizar)),
                credito.strip(),
                de,
                ate,
                _now(),
            ),
        )
        row = conn.execute("SELECT * FROM autorizacoes WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _autorizacao_dict(row)


def revogar_autorizacao(autorizacao_id: int, db_path: Path | None = None) -> dict[str, Any]:
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM autorizacoes WHERE id = ?", (autorizacao_id,)).fetchone()
        if not row:
            raise AutorizacaoError("Autorização não encontrada.")
        if not row["revogada_em"]:
            conn.execute("UPDATE autorizacoes SET revogada_em = ? WHERE id = ?", (_now(), autorizacao_id))
        row = conn.execute("SELECT * FROM autorizacoes WHERE id = ?", (autorizacao_id,)).fetchone()
    return _autorizacao_dict(row)


def obter_criador(criador_id: int, db_path: Path | None = None, hoje: date | None = None) -> dict[str, Any] | None:
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM criadores WHERE id = ?", (criador_id,)).fetchone()
        if not row:
            return None
        auts = conn.execute(
            "SELECT * FROM autorizacoes WHERE criador_id = ? ORDER BY id DESC", (criador_id,)
        ).fetchall()
    criador = _criador_dict(row)
    criador["autorizacoes"] = [_autorizacao_dict(a, hoje) for a in auts]
    criador["autorizado"] = any(a["valida"] for a in criador["autorizacoes"])
    return criador


def listar_criadores(db_path: Path | None = None, hoje: date | None = None) -> list[dict[str, Any]]:
    with _conn(db_path) as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM criadores ORDER BY nome COLLATE NOCASE")]
    return [c for c in (obter_criador(i, db_path, hoje) for i in ids) if c]


def autorizacao_vigente(
    criador_id: int, db_path: Path | None = None, hoje: date | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Devolve (criador, autorização válida mais recente) ou levanta AutorizacaoError."""
    criador = obter_criador(criador_id, db_path, hoje)
    if not criador:
        raise AutorizacaoError("Criador não encontrado.")
    validas = [a for a in criador["autorizacoes"] if a["valida"]]
    if not validas:
        raise AutorizacaoError(
            f"{criador['nome']} não tem autorização válida. Cadastre a autorização antes de baixar lives."
        )
    return criador, validas[0]


def canal_confere(criador: dict[str, Any], info: dict[str, Any]) -> bool:
    """True se a live é de um canal cadastrado do criador.

    Sem canais cadastrados não há o que comparar e a live passa (o cadastro
    do canal é recomendado, mas não obrigatório).
    """
    canais = set(criador.get("canais") or [])
    if not canais:
        return True
    candidatos = {
        normalizar_canal(str(info.get(key) or ""))
        for key in ("channel_id", "uploader_id", "channel_url", "uploader_url")
    }
    candidatos.discard("")
    return bool(canais & candidatos)
