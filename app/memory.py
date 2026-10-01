"""
Memoria persistente del bot por cliente y global.

- aliases: modelo que el cliente dijo → modelo real Safelite (por marca)
- client_memory: qué funcionó/falló por PSID (no repetir errores con el mismo cliente)
- global_patterns: frases que ganaron/perdieron globalmente (aprende de todos)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from threading import Lock

log = logging.getLogger("memory")

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "leads.db"
_lock = Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def init_memory() -> None:
    """Crea las tablas si no existen."""
    from . import legacy
    with legacy.db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS bot_aliases (
                make_key TEXT NOT NULL,
                said_key TEXT NOT NULL,
                real_model TEXT NOT NULL,
                psid TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                uses INTEGER DEFAULT 1,
                PRIMARY KEY (make_key, said_key)
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS client_memory (
                psid TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detail TEXT DEFAULT '',
                worked INTEGER DEFAULT 0,
                created_at TEXT DEFAULT ''
            )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_client_memory_psid ON client_memory(psid)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_client_memory_type ON client_memory(event_type)"
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS global_patterns (
                fingerprint TEXT PRIMARY KEY,
                action TEXT DEFAULT '',
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                example TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )"""
        )


# ---------------------------------------------------------------------------
# Aliases: modelo dicho → modelo real
# ---------------------------------------------------------------------------

def remember_alias(make: str, said: str, real: str, psid: str = "") -> bool:
    """Guarda que el cliente dijo 'hardbody' pero Safelite lo llama '850 Pickup'."""
    mk, sd, rl = (make or "").strip(), (said or "").strip(), (real or "").strip()
    if not mk or not sd or not rl or _key(sd) == _key(rl):
        return False
    mk_k, sd_k = _key(mk), _key(sd)
    if not mk_k or not sd_k or len(sd_k) < 3:
        return False
    init_memory()
    from . import legacy
    with legacy.db() as c:
        c.execute(
            """INSERT INTO bot_aliases(make_key, said_key, real_model, psid, created_at, updated_at, uses)
                VALUES(?,?,?,?,?,?,1)
                ON CONFLICT(make_key, said_key) DO UPDATE SET
                    real_model=excluded.real_model,
                    psid=excluded.psid,
                    updated_at=excluded.updated_at,
                    uses=bot_aliases.uses+1
            """,
            (mk_k, sd_k, rl, psid, _now(), _now()),
        )
    return True


def get_alias(make: str, said: str) -> str:
    """Devuelve el modelo real si ya lo aprendimos, o '' si no."""
    mk_k, sd_k = _key(make), _key(said)
    if not mk_k or not sd_k:
        return ""
    init_memory()
    from . import legacy
    with legacy.db() as c:
        r = c.execute(
            "SELECT real_model FROM bot_aliases WHERE make_key=? AND said_key=?",
            (mk_k, sd_k),
        ).fetchone()
    return r["real_model"] if r else ""


def migrate_aliases_from_json() -> int:
    """Migra aliases_aprendidos.json a SQLite. Se puede correr mil veces."""
    json_path = BASE / "aliases_aprendidos.json"
    if not json_path.exists():
        return 0
    try:
        data = json.loads(json_path.read_text(encoding="utf-8") or "{}")
        by = data.get("by_make") if isinstance(data, dict) else {}
        n = 0
        for make_key, models in by.items():
            if not isinstance(models, dict):
                continue
            for said_key, real in models.items():
                if not isinstance(real, str) or not real.strip():
                    continue
                if remember_alias(make_key, said_key, real):
                    n += 1
        log.info("aliases migrados: %d", n)
        return n
    except Exception:
        log.exception("migrate aliases")
        return 0


# ---------------------------------------------------------------------------
# Memoria por cliente: qué funcionó/falló con ESTE cliente
# ---------------------------------------------------------------------------

def client_event(psid: str, event_type: str, detail: str = "", worked: bool = False) -> None:
    """Guarda un evento de aprendizaje por cliente."""
    if not psid:
        return
    init_memory()
    from . import legacy
    with legacy.db() as c:
        c.execute(
            """INSERT INTO client_memory(psid, event_type, detail, worked, created_at)
                VALUES(?,?,?,?,?)""",
            (psid, event_type, (detail or "")[:200], 1 if worked else 0, _now()),
        )


def client_history(psid: str, limit: int = 20) -> list[dict]:
    """Devuelve los últimos eventos de aprendizaje de este cliente."""
    init_memory()
    from . import legacy
    with legacy.db() as c:
        rows = c.execute(
            "SELECT event_type, detail, worked, created_at FROM client_memory "
            "WHERE psid=? ORDER BY created_at DESC LIMIT ?",
            (psid, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def client_failed_before(psid: str, event_type: str) -> bool:
    """True si este cliente ya tuvo un fallo de este tipo (no repetir el mismo error)."""
    init_memory()
    from . import legacy
    with legacy.db() as c:
        r = c.execute(
            "SELECT COUNT(*) as n FROM client_memory WHERE psid=? AND event_type=? AND worked=0",
            (psid, event_type),
        ).fetchone()
    return (r["n"] or 0) > 0


# ---------------------------------------------------------------------------
# Patrones globales: frases que ganaron/perdieron con TODOS los clientes
# ---------------------------------------------------------------------------

def _fingerprint(text: str) -> str:
    from .faq_extra import _norm
    t = _norm(text or "")
    t = re.sub(r"[^a-z0-9ñ ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    if re.fullmatch(r"\d{5}", t):
        return ""
    words = t.split()
    return " ".join(words[:8])[:80]


def learn_pattern(text: str, action: str, worked: bool, example: str = "") -> None:
    """Aprende que esta frase → esta acción funcionó o falló."""
    fp = _fingerprint(text)
    if not fp or not action:
        return
    init_memory()
    from . import legacy
    col = "wins" if worked else "losses"
    with legacy.db() as c:
        c.execute(
            f"""INSERT INTO global_patterns(fingerprint, action, wins, losses, example, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    action=excluded.action,
                    {col}={col}+1,
                    example=COALESCE(NULLIF(excluded.example,''), global_patterns.example),
                    updated_at=excluded.updated_at
            """,
            (fp, action, 1 if worked else 0, 0 if worked else 1, (example or text)[:160], _now()),
        )


def best_action(text: str) -> str | None:
    """Devuelve la acción ganadora para esta frase, o None si no hay suficiente evidencia."""
    fp = _fingerprint(text)
    if not fp:
        return None
    init_memory()
    from . import legacy
    with legacy.db() as c:
        r = c.execute(
            "SELECT action, wins, losses FROM global_patterns WHERE fingerprint=?",
            (fp,),
        ).fetchone()
    if not r:
        return None
    wins, losses = int(r["wins"] or 0), int(r["losses"] or 0)
    if wins < 2 or wins <= losses:
        return None
    return r["action"]


# ---------------------------------------------------------------------------
# Bootstrap: migrar datos viejos y aprender del historial
# ---------------------------------------------------------------------------

def bootstrap() -> None:
    """Migra aliases viejos y aprende del historial de leads."""
    init_memory()
    n = migrate_aliases_from_json()
    log.info("bootstrap: %d aliases migrados", n)

    from . import legacy
    for lead in legacy.list_leads():
        psid = str(lead.get("psid") or "")
        if not psid:
            continue
        # Aprender aliases del historial de este lead
        try:
            hist = json.loads(lead.get("history") or "[]")
        except Exception:
            hist = []
        users = [str(m.get("content") or "") for m in hist if (m.get("role") or "") == "user"]
        if users and lead.get("make") and lead.get("model"):
            from .legacy import extract_vehicle_from_text
            for said in users[:8]:
                if not said or len(said) > 48 or len(said.split()) > 5:
                    continue
                veh = extract_vehicle_from_text(said) or {}
                dirty = (veh.get("model") or "").strip()
                real = str(lead.get("model") or "").strip()
                if dirty and real and dirty.lower() != real.lower():
                    remember_alias(str(lead.get("make")), dirty, real, psid=psid)
        # Aprender patrones del historial
        won = bool(legacy._has_vehicle(lead) or legacy._lead_zip(lead) or lead.get("slots_json"))
        for u in users[:6]:
            if not _fingerprint(u):
                continue
            from .faq_extra import detectar
            action = detectar(u) or "ask_car"
            learn_pattern(u, action, worked=won, example=u)
